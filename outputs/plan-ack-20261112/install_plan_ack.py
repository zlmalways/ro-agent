#!/usr/bin/env python3
"""Install the reviewed plan acknowledgement patch; --check is read-only."""
import argparse
import ast
import getpass
import hashlib
import io
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import wave

PATCHES = json.loads('{"src/deepleap_robot_skills/deepleap_robot_skills/command_bridge.py": {"before": "df1105dbbc17a4ac14214294b402156a4254498676e0749753b85a7d45cc6eb8", "after": "ec9f5674bcd3e2f54b832782797d588666e0949da6a563ea984cd75c26d88c16", "edits": [["        self._inbox: queue.Queue = queue.Queue()\\n", "        self._inbox: queue.Queue = queue.Queue()\\n        # PLAN_ACK_V1: notification only; never changes action acceptance.\\n        self._plan_ack_pub = self.create_publisher(String, \'/deepleap/plan_ack\', 10)\\n"], ["        gfut.add_done_callback(lambda f: self._on_goal_response(f, cmd_id))\\n\\n    def _on_goal_response(self, gfut, cmd_id: str) -> None:", "        plan_id = frame.get(\'plan_id\')\\n        gfut.add_done_callback(lambda f: self._on_goal_response(f, cmd_id, plan_id))\\n\\n    def _on_goal_response(self, gfut, cmd_id: str, plan_id=None) -> None:"], ["        if self._gate.on_goal_response(True):\\n", "        cancel_pending = self._gate.on_goal_response(True)\\n        if cancel_pending:\\n"], ["        rfut.add_done_callback(lambda f: self._on_result(f, cmd_id))\\n\\n    def _on_result", "        rfut.add_done_callback(lambda f: self._on_result(f, cmd_id))\\n        if not cancel_pending:\\n            self._notify_plan_ack(plan_id, cmd_id)\\n\\n    def _notify_plan_ack(self, plan_id, cmd_id):\\n        # PLAN_ACK_V1: missing metadata skips speech, never the physical action.\\n        try:\\n            if not isinstance(plan_id, str) or not plan_id.strip() or len(plan_id) > 200:\\n                self.get_logger().warn(f\'plan_ack skipped: missing/invalid plan_id cmd={cmd_id}\')\\n                return\\n            if self._plan_ack_pub.get_subscription_count() == 0:\\n                self.get_logger().warn(f\'plan_ack skipped: voice subscriber unavailable cmd={cmd_id}\')\\n                return\\n            msg = String()\\n            msg.data = json.dumps({\'plan_id\': plan_id, \'cmd_id\': cmd_id})\\n            self._plan_ack_pub.publish(msg)\\n            self.get_logger().info(f\'plan_ack notified plan={plan_id} cmd={cmd_id}\')\\n        except Exception as exc:\\n            self.get_logger().error(f\'plan_ack notification failed: {type(exc).__name__}; action continues\')\\n\\n    def _on_result"]]}, "src/deepleap_robot_voice/deepleap_robot_voice/mic_vad_sender_node.py": {"before": "a849e2bf30bb65a9aa3bdc6f54c7da4b7e3d3839aac263087fdaba9f488bd61f", "after": "197a7a1e1fea4a479ef44756c325fb3b79c93407bfaafb0531849aae4b1855eb", "edits": [["import threading\\n", "import threading\\nimport sqlite3\\nfrom pathlib import Path\\n"], ["class MicVadSenderNode(Node):\\n", "\\nclass PlanAckAudio:\\n    \\"\\"\\"PLAN_ACK_V1: durable, at-most-once playback admission per plan.\\n\\n    Claim is committed before enqueueing. A crash/stop/queue failure may omit\\n    the acknowledgement, but later steps/restarts must not replay that plan.\\n    This is a playback attempt record, not proof of audible output.\\n    \\"\\"\\"\\n\\n    def __init__(self, directory):\\n        self.directory = Path(directory)\\n        self._audio = None\\n\\n    def claim(self, plan_id, cmd_id):\\n        if not (self.directory / \'plan_ack.enabled\').is_file():\\n            return None\\n        for value in (plan_id, cmd_id):\\n            if not isinstance(value, str) or not value.strip() or len(value) > 200:\\n                raise ValueError(\'invalid plan/command identifier\')\\n        if self._audio is None:\\n            path = self.directory / \'plan_ack.wav\'\\n            if path.stat().st_size > 2_100_000:\\n                raise ValueError(\'plan acknowledgement WAV exceeds size limit\')\\n            with wave.open(str(path), \'rb\') as wav:\\n                channels, width, rate, frames = (\\n                    wav.getnchannels(), wav.getsampwidth(),\\n                    wav.getframerate(), wav.getnframes())\\n                if (channels, width, rate) != (1, 2, 24000) or not 0 < frames <= rate * 10:\\n                    raise ValueError(\'expected mono PCM16 24kHz WAV, at most 10 seconds\')\\n                raw = wav.readframes(frames)\\n            if len(raw) != frames * width:\\n                raise ValueError(\'incomplete acknowledgement WAV\')\\n            self._audio = (np.frombuffer(raw, dtype=\'<i2\').astype(np.float32) / 32768.0, rate)\\n        db = sqlite3.connect(str(self.directory / \'plan_ack.sqlite3\'), timeout=0.2)\\n        try:\\n            with db:\\n                db.execute(\'CREATE TABLE IF NOT EXISTS playback_claims (\'\\n                           \'plan_id TEXT PRIMARY KEY, cmd_id TEXT NOT NULL, \'\\n                           \'claimed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)\')\\n                changed = db.execute(\\n                    \'INSERT OR IGNORE INTO playback_claims (plan_id,cmd_id) VALUES (?,?)\',\\n                    (plan_id, cmd_id)).rowcount\\n        finally:\\n            db.close()\\n        return self._audio if changed == 1 else None\\n\\n\\nclass MicVadSenderNode(Node):\\n"], ["        url = self._ws_url + (\'&\' if \'?\' in self._ws_url else \'?\') + f\'robot_id={self._robot_id}\'\\n", "        # PLAN_ACK_V1: local asset and opt-in marker; reuse paced playback/mic mute.\\n        self._plan_ack = PlanAckAudio(Path.home() / \'.local/share/deepleap-robot/audio\')\\n        self._plan_ack_sub = self.create_subscription(\\n            String, \'/deepleap/plan_ack\', self._on_plan_ack, 10)\\n        self.get_logger().info(f\'plan_ack configuration directory: {self._plan_ack.directory}\')\\n\\n        url = self._ws_url + (\'&\' if \'?\' in self._ws_url else \'?\') + f\'robot_id={self._robot_id}\'\\n"], ["    def _on_frame(self, d: dict):\\n", "    def _on_plan_ack(self, msg):\\n        try:\\n            if len(msg.data) > 1024:\\n                raise ValueError(\'oversized plan acknowledgement\')\\n            event = json.loads(msg.data)\\n            plan_id, cmd_id = event.get(\'plan_id\'), event.get(\'cmd_id\')\\n            # Snapshot before disk work; a concurrent stop/reconnect invalidates it.\\n            gen = self._play_gen\\n            audio = self._plan_ack.claim(plan_id, cmd_id)\\n            if audio is None:\\n                self.get_logger().info(f\'plan_ack skipped: disabled/already claimed plan={plan_id}\')\\n                return\\n            if gen != self._play_gen:\\n                self.get_logger().warn(f\'plan_ack interrupted before enqueue plan={plan_id}\')\\n                return\\n            self._extend_mute(self._mute_tail)\\n            self._audio_q.put_nowait((audio[0], audio[1], gen))\\n            try:\\n                self._audio_q.put_nowait(None)\\n            except queue.Full:\\n                pass  # Existing bounded idle timeout closes playback if no sentinel fits.\\n            self.get_logger().info(f\'plan_ack queued plan={plan_id} cmd={cmd_id}\')\\n        except Exception as exc:\\n            self.get_logger().error(f\'plan_ack failed: {type(exc).__name__}: {exc}; action continues\')\\n\\n    def _on_frame(self, d: dict):\\n"], ["            finally:\\n                # P4 过渡窗:先设尾罩再放 _playing,防两赋值间隙漏进带回声的帧;", "            except Exception as exc:\\n                self.get_logger().error(\\n                    f\'audio playback failed: {type(exc).__name__}: {exc}; playback worker continues\')\\n            finally:\\n                # P4 过渡窗:先设尾罩再放 _playing,防两赋值间隙漏进带回声的帧;"]]}}')
WAV_SHA256 = 'c4b5cdc6891c5bd4fb7655359ab6bd864d0a05c7b0421fbddf16b4bd55d8f876'

def normalized(raw):
    return raw.decode('utf-8').replace('\r\n', '\n')

def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

def patched(text, edits):
    for old, new in edits:
        if text.count(old) != 1:
            raise RuntimeError('Patch anchor differs; no files changed')
        text = text.replace(old, new, 1)
    ast.parse(text)
    return text

def prepare(repo):
    pending = []
    for name, spec in PATCHES.items():
        path = (repo / name).resolve(strict=True)
        if not path.is_relative_to(repo):
            raise RuntimeError('Source path escapes repository')
        raw = path.read_bytes()
        text = normalized(raw)
        actual = digest(text)
        if actual == spec['after']:
            print('ALREADY_APPLIED', name)
            continue
        if actual != spec['before']:
            raise RuntimeError('File differs from reviewed baseline: ' + name)
        after = patched(text, spec['edits'])
        if digest(after) != spec['after']:
            raise RuntimeError('Unexpected patch output')
        newline = '\r\n' if b'\r\n' in raw else '\n'
        data = after.replace('\n', newline).encode('utf-8')
        pending.append((path, raw, data))
        print('PATCH_READY', name)
    return pending

def atomic_write(path, data):
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(prefix='.plan-ack-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def validate_audio(path):
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != WAV_SHA256:
        raise RuntimeError('WAV checksum differs')
    with wave.open(io.BytesIO(data), 'rb') as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 24000):
            raise RuntimeError('WAV format differs')
        if not 0 < wav.getnframes() <= 240000:
            raise RuntimeError('WAV length differs')
    return data

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path('/home/astribot/cj/deepleap-robot'))
    parser.add_argument('--wav', type=Path, default=Path(__file__).with_name('ack_good_go.wav'))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    repo = args.repo.resolve(strict=True)
    pending = prepare(repo)
    audio = validate_audio(args.wav)
    print('WAV_PASS', len(audio), 'bytes')
    if args.check:
        print('CHECK_PASS: no files changed; no service restarted')
        return
    expected_repo = Path('/home/astribot/cj/deepleap-robot').resolve(strict=True)
    if repo != expected_repo or getpass.getuser() != 'astribot':
        raise RuntimeError('Run as astribot in the approved Orin repository; do not use sudo')
    service_user = subprocess.check_output(
        ['systemctl', 'show', 'deepleap-robot.service', '-p', 'User', '--value'],
        text=True).strip()
    if service_user != 'astribot':
        raise RuntimeError('Service User is not astribot; stop and report before deployment')
    branch = subprocess.check_output(
        ['git', '-C', str(repo), 'branch', '--show-current'], text=True).strip()
    head = subprocess.check_output(
        ['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    if branch != 'feat/brain-wiring' or head != 'a2e834731d541dfc04a405a6ac030da121c2f403':
        raise RuntimeError('Branch/commit differs from confirmed Orin baseline: ' + branch + ' ' + head)
    for module, name in (
        ('deepleap_robot_skills.command_bridge',
         'src/deepleap_robot_skills/deepleap_robot_skills/command_bridge.py'),
        ('deepleap_robot_voice.mic_vad_sender_node',
         'src/deepleap_robot_voice/deepleap_robot_voice/mic_vad_sender_node.py'),
    ):
        spec = importlib.util.find_spec(module)
        if spec is None or Path(spec.origin).resolve() != (repo / name).resolve():
            raise RuntimeError('Runtime source mismatch; source the ROS and robot setup.bash first: ' + module)
    home = Path.home().resolve()
    if home != Path('/home/astribot'):
        raise RuntimeError('Unexpected HOME')
    asset_dir = home / '.local/share/deepleap-robot/audio'
    target = asset_dir / 'plan_ack.wav'
    enabled = asset_dir / 'plan_ack.enabled'
    if target.exists() and target.read_bytes() != audio:
        raise RuntimeError('A different plan_ack.wav exists; refusing overwrite')
    for path, before, after in pending:
        if path.read_bytes() != before:
            raise RuntimeError('Concurrent source modification; nothing applied')
    changed = []
    try:
        for path, before, after in pending:
            if path.read_bytes() != before:
                raise RuntimeError('Concurrent source modification')
            atomic_write(path, after)
            changed.append((path, before, after))
        asset_dir.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            with target.open('xb') as stream:
                stream.write(audio)
        if target.read_bytes() != audio:
            raise RuntimeError('Copied WAV verification failed')
        for name, spec in PATCHES.items():
            if digest(normalized((repo / name).read_bytes())) != spec['after']:
                raise RuntimeError('Post-install source verification failed')
        enabled.touch(exist_ok=True)
    except Exception:
        for path, before, after in reversed(changed):
            if path.read_bytes() == after:
                atomic_write(path, before)
                print('ROLLED_BACK_SOURCE', path)
            else:
                print('CONCURRENT_CHANGE_PRESERVED', path)
        print('If audio assets were created, they remain at', asset_dir)
        raise
    print('INSTALL_PASS: two source files and local audio; service NOT restarted')
    print('AUDIO', target)
    print('ENABLE_MARKER', enabled)
    print('CLAIMS_DB (created on first event)', asset_dir / 'plan_ack.sqlite3')
    print('Existing robot.launch.py, brain completion and V11 files were not modified.')
    print('Restart and real end-to-end acceptance are still pending.')

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('STOP:', str(exc))
        raise SystemExit(1)
