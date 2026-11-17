#!/usr/bin/env python3
"""Development checks only; does not exercise ROS, a speaker, or physical actions."""
import ast
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import wave

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
ROBOT = Path(r'D:\codex_chat\2026-10-25\new-chat\deepleap-robot')
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'src'))

def module_from_path(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def main():
    installer = module_from_path('plan_ack_install', HERE / 'install_plan_ack.py')
    pending = installer.prepare(ROBOT.resolve())
    assert len(pending) == 2
    wav_bytes = installer.validate_audio(HERE / 'ack_good_go.wav')
    patched = {str(path.relative_to(ROBOT)).replace('\\', '/'): data.decode('utf-8').replace('\r\n', '\n')
               for path, before, data in pending}
    for path, before, data in pending:
        assert path.read_bytes() == before
        compile(data.decode('utf-8'), str(path), 'exec')
    print('PASS: both robot source patches compile; original files unchanged')
    for name, source in patched.items():
        assert installer.digest(source) == installer.PATCHES[name]['after']
        assert installer.digest(source) != installer.PATCHES[name]['before']
    print('PASS: patched source hashes match the reviewed post-install hashes')

    from robot_agent.domain.models import Command
    from robot_agent.protocol.dlv0_schema import command_frame
    sys.path.insert(0, str(ROBOT / 'src/deepleap_robot_skills'))
    from deepleap_robot_skills import dlv_protocol as dlv
    frames = []
    for cmd_id, plan_id, step in (
        ('TEST-c1', 'TEST-plan-1', 'step-1'),
        ('TEST-c2', 'TEST-plan-1', 'step-2'),
        ('TEST-c3', 'TEST-plan-2', 'step-1'),
    ):
        command = Command(cmd_id, 'TEST-task', 'TEST-customer', 'TEST-unit', plan_id,
                          step, 'TEST-binding', 'move_to', {'location': 'TEST-location'}, 1)
        wire = command_frame(command)
        parsed = dlv.parse_frame(json.dumps(wire))
        assert dlv.parse_command(parsed) == (cmd_id, 'move_to', {'location': 'TEST-location'})
        assert parsed['plan_id'] == plan_id
        frames.append(parsed)
    assert frames[0]['plan_id'] == frames[1]['plan_id'] != frames[2]['plan_id']
    print('PASS: actual platform frame stays compatible with unchanged robot parser; plan IDs retained')

    voice = patched['src/deepleap_robot_voice/deepleap_robot_voice/mic_vad_sender_node.py']
    tree = ast.parse(voice)
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'PlanAckAudio')
    namespace = {'Path': Path, 'np': np, 'sqlite3': sqlite3, 'wave': wave}
    # Run the real pure asset/dedup class, without importing ROS or replacing its logic.
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<real PlanAckAudio class>', 'exec'), namespace)
    with tempfile.TemporaryDirectory(prefix='TEST-plan-ack-', dir=HERE) as temp:
        directory = Path(temp)
        (directory / 'ack_good_go.wav').write_bytes(wav_bytes)
        (directory / 'ack_good_go.wav').rename(directory / 'plan_ack.wav')
        enabled = directory / 'plan_ack.enabled'
        enabled.touch()
        admit = namespace['PlanAckAudio'](directory)
        first = admit.claim('TEST-plan-1', 'TEST-c1')
        assert first is not None and first[1] == 24000 and first[0].size > 0
        assert admit.claim('TEST-plan-1', 'TEST-c1') is None
        assert admit.claim('TEST-plan-1', 'TEST-c2') is None
        restarted = namespace['PlanAckAudio'](directory)
        assert restarted.claim('TEST-plan-1', 'TEST-c2') is None
        assert restarted.claim('TEST-plan-2', 'TEST-c3') is not None
        with closing(sqlite3.connect(str(directory / 'plan_ack.sqlite3'))) as db:
            rows = db.execute('SELECT plan_id,cmd_id FROM playback_claims ORDER BY plan_id').fetchall()
        assert rows == [('TEST-plan-1', 'TEST-c1'), ('TEST-plan-2', 'TEST-c3')]
        enabled.unlink()
        assert restarted.claim('TEST-plan-3', 'TEST-c4') is None
        with closing(sqlite3.connect(str(directory / 'plan_ack.sqlite3'))) as db:
            assert db.execute('SELECT COUNT(*) FROM playback_claims').fetchone()[0] == 2
    assert not directory.exists()
    print('PASS: real WAV decoding and SQLite claims; duplicate command/later step/recreated instance skipped')
    print('PASS: a new plan is admitted; disable marker prevents new claims')
    print('PASS: temporary TEST directory removed; no test logs retained')
    print('UNVERIFIED: ROS delivery, audible playback, microphone muting and page-to-robot real task flow')

if __name__ == '__main__':
    main()
