const talkBtn = document.getElementById('talk-btn');
const talkBtnLabel = document.getElementById('talk-btn-label');
const statusText = document.getElementById('status');
const meter = document.getElementById('meter');
const meterBars = meter.querySelectorAll('.bar');
const connStatus = document.getElementById('conn-status');
const connText = document.getElementById('conn-text');
const chatBox = document.getElementById('chat-box');

let socket = null;
let audioContext = null;
let mediaStream = null;
let scriptProcessor = null;
let analyser = null;
let meterRAF = null;
let isStreaming = false;

// Playback queue state
let playbackAudioContext = null;
let audioQueue = [];
let isPlayingQueue = false;
let activeAudioSource = null;

let currentAssistantBubble = null;
let emptyPlaceholder = null;

function initPlaybackContext() {
  if (!playbackAudioContext) {
    playbackAudioContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 24000 });
  }
  if (playbackAudioContext.state === 'suspended') {
    playbackAudioContext.resume();
  }
}

function stopAllAudioPlayback() {
  audioQueue = [];
  isPlayingQueue = false;
  if (activeAudioSource) {
    try { activeAudioSource.stop(); } catch (e) { /* already stopped */ }
    activeAudioSource = null;
  }
  meter.classList.remove('bot');
}

async function queueAudioChunk(arrayBuffer) {
  initPlaybackContext();
  try {
    const audioBuffer = await playbackAudioContext.decodeAudioData(arrayBuffer);
    audioQueue.push(audioBuffer);
    processAudioQueue();
  } catch (e) {
    console.error('Error decoding audio chunk:', e);
  }
}

function processAudioQueue() {
  if (isPlayingQueue || audioQueue.length === 0) return;
  isPlayingQueue = true;
  meter.classList.add('bot');
  talkBtn.classList.add('bot-speaking');

  const audioBuffer = audioQueue.shift();
  activeAudioSource = playbackAudioContext.createBufferSource();
  activeAudioSource.buffer = audioBuffer;
  activeAudioSource.connect(playbackAudioContext.destination);

  activeAudioSource.onended = () => {
    isPlayingQueue = false;
    activeAudioSource = null;
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send('AUDIO_ACK');
    }
    if (audioQueue.length === 0) {
      meter.classList.remove('bot');
      talkBtn.classList.remove('bot-speaking');
    }
    processAudioQueue();
  };

  activeAudioSource.start(0);
}

function convertFloat32ToInt16(buffer) {
  let l = buffer.length;
  let buf = new Int16Array(l);
  while (l--) {
    let s = Math.max(-1, Math.min(1, buffer[l]));
    buf[l] = s < 0 ? s * 0x8000 : s * 0x7FFF;
  }
  return buf.buffer;
}

// Drives the LED-style level meter from the mic input, independent of playback.
function startMeter(sourceAnalyser) {
  const data = new Uint8Array(sourceAnalyser.frequencyBinCount);
  const bars = Array.from(meterBars);

  function tick() {
    sourceAnalyser.getByteFrequencyData(data);
    const step = Math.floor(data.length / bars.length);
    bars.forEach((bar, i) => {
      const v = data[i * step] / 255;
      bar.style.height = `${6 + v * 34}px`;
    });
    meterRAF = requestAnimationFrame(tick);
  }
  tick();
}

function stopMeter() {
  if (meterRAF) cancelAnimationFrame(meterRAF);
  meterRAF = null;
  meterBars.forEach(bar => { bar.style.height = '6px'; });
  meter.classList.remove('listening', 'bot');
}

function clearEmptyPlaceholder() {
  if (emptyPlaceholder) {
    emptyPlaceholder.remove();
    emptyPlaceholder = null;
  }
}

async function startStreaming() {
  try {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    socket = new WebSocket(`${protocol}//${window.location.host}/ws`);
    socket.binaryType = 'arraybuffer';

    socket.onopen = async () => {
      isStreaming = true;
      statusText.innerText = 'Listening — go ahead and order';
      talkBtn.classList.add('on');
      talkBtnLabel.innerText = 'Tap to stop';
      connStatus.classList.add('live');
      connText.innerText = 'Connected';

      mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      audioContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
      const source = audioContext.createMediaStreamSource(mediaStream);

      analyser = audioContext.createAnalyser();
      analyser.fftSize = 256;
      source.connect(analyser);
      meter.classList.add('listening');
      startMeter(analyser);

      scriptProcessor = audioContext.createScriptProcessor(4096, 1, 1);
      scriptProcessor.onaudioprocess = (e) => {
        if (socket && socket.readyState === WebSocket.OPEN) {
          const inputData = e.inputBuffer.getChannelData(0);
          const pcmBuffer = convertFloat32ToInt16(inputData);
          socket.send(pcmBuffer);
        }
      };

      source.connect(scriptProcessor);
      scriptProcessor.connect(audioContext.destination);
    };

    socket.onmessage = async (event) => {
      if (event.data instanceof ArrayBuffer) {
        await queueAudioChunk(event.data);
        return;
      }

      const data = event.data;

      if (data === 'STOP_AUDIO') {
        stopAllAudioPlayback();
        return;
      }

      if (data === 'ASSISTANT_INTERRUPTED') {
        if (currentAssistantBubble) {
          currentAssistantBubble.classList.add('interrupted');
        }
        currentAssistantBubble = null;
        return;
      }

      if (data.startsWith('USER:')) {
        clearEmptyPlaceholder();
        const bubble = document.createElement('div');
        bubble.className = 'bubble user';
        bubble.innerText = data.replace('USER:', '').trim();
        chatBox.appendChild(bubble);
        currentAssistantBubble = null;
      } else if (data.startsWith('ASSISTANT_CHUNK:')) {
        clearEmptyPlaceholder();
        const chunk = data.replace('ASSISTANT_CHUNK: ', '');
        if (!currentAssistantBubble) {
          currentAssistantBubble = document.createElement('div');
          currentAssistantBubble.className = 'bubble bot';
          chatBox.appendChild(currentAssistantBubble);
        }
        currentAssistantBubble.innerText += chunk;
      } else if (data === 'ASSISTANT_DONE') {
        currentAssistantBubble = null;
      }

      chatBox.scrollTop = chatBox.scrollHeight;
    };

    socket.onclose = () => { stopStreaming(); };
    socket.onerror = (err) => { console.error('WebSocket error:', err); stopStreaming(); };

  } catch (err) {
    console.error('Microphone error:', err);
    statusText.innerText = 'Microphone access denied';
  }
}

function stopStreaming() {
  isStreaming = false;
  stopAllAudioPlayback();
  stopMeter();
  if (scriptProcessor) scriptProcessor.disconnect();
  if (analyser) analyser.disconnect();
  if (audioContext) audioContext.close();
  if (mediaStream) mediaStream.getTracks().forEach(track => track.stop());
  if (socket && socket.readyState === WebSocket.OPEN) socket.close();

  statusText.innerText = 'Press the button and start ordering';
  talkBtn.classList.remove('on', 'bot-speaking');
  talkBtnLabel.innerText = 'Tap to talk';
  connStatus.classList.remove('live');
  connText.innerText = 'Not connected';
}

talkBtn.addEventListener('click', () => {
  if (isStreaming) {
    stopStreaming();
  } else {
    startStreaming();
  }
});