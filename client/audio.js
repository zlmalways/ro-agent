'use strict';

// Decode the browser's native recording, mix down and resample before writing PCM.
async function recordingToWav(blob) {
  const decoder = new AudioContext();
  let decoded;
  try {
    decoded = await decoder.decodeAudioData(await blob.arrayBuffer());
  } finally {
    await decoder.close();
  }
  const rate = 16000;
  const context = new OfflineAudioContext(1, Math.max(1, Math.ceil(decoded.duration * rate)), rate);
  const source = context.createBufferSource();
  source.buffer = decoded;
  source.connect(context.destination);
  source.start();
  const samples = (await context.startRendering()).getChannelData(0);
  const data = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(data);

  function tag(offset, value) {
    for (let i = 0; i < value.length; i++) view.setUint8(offset + i, value.charCodeAt(i));
  }
  tag(0, 'RIFF');
  view.setUint32(4, data.byteLength - 8, true);
  tag(8, 'WAVE');
  tag(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, rate, true);
  view.setUint32(28, rate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  tag(36, 'data');
  view.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) {
    const sample = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(44 + i * 2, Math.round(sample * (sample < 0 ? 32768 : 32767)), true);
  }
  return new File([data], 'recording.wav', {
    type: 'audio/wav'
  });
}

async function captureMicrophone() {
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    throw new Error('当前浏览器无法录音，请使用 WAV 文件。');
  }
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: true
  });
  const release = () => stream.getTracks().forEach(track => track.stop());
  try {
    const recorder = new MediaRecorder(stream);
    const chunks = [];
    recorder.ondataavailable = event => {
      if (event.data.size) chunks.push(event.data);
    };
    const stopped = new Promise((resolve, reject) => {
      recorder.onstop = () => {
        release();
        resolve(new Blob(chunks, {
          type: recorder.mimeType
        }));
      };
      recorder.onerror = () => {
        release();
        reject(new Error('录音中断，请重新录制。'));
      };
    });
    recorder.start();
    return {
      stop() {
        if (recorder.state !== 'inactive') recorder.stop();
      },
      stopped
    };
  } catch (error) {
    release();
    throw error;
  }
}
