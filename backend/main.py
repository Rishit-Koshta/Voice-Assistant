import os
import io
import wave
import asyncio
import numpy as np
import torch
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from dotenv import load_dotenv
from groq import AsyncGroq
import edge_tts

load_dotenv()

app = FastAPI(title="Barge-In Voice Assistant")
app.mount("/static", StaticFiles(directory="frontend"), name="static")

groq_client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))

print("Loading Silero VAD model...")
model, utils = torch.hub.load(
    repo_or_dir='snakers4/silero-vad',
    model='silero_vad',
    force_reload=False,
    trust_repo=True
)
model.eval()
print("✅ Silero VAD loaded successfully!")

def create_wav_buffer(pcm_bytes: bytes, sample_rate: int = 16000) -> io.BytesIO:
    wav_io = io.BytesIO()
    with wave.open(wav_io, 'wb') as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm_bytes)
    wav_io.seek(0)
    return wav_io

@app.get("/")
async def get_index():
    with open("frontend/index.html", "r") as f:
        return HTMLResponse(content=f.read())

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    print("🟢 Client connected via WebSocket")
    
    speech_buffer = bytearray()
    is_speaking = False
    silence_chunks = 0
    SILENCE_THRESHOLD_CHUNKS = 45  # ~1.4 seconds of silence
    
    is_bot_speaking = False
    pending_audio_chunks = 0  # chunks sent to the client but not yet confirmed played
    current_response_task = None
    
    chat_history = [
        {
            "role": "system",
            "content": (
                "You are a friendly, fast-paced drive-thru order taker at IT Geeks Burger Joint. "
                "Keep your answers concise, conversational, and natural for speech (no bullet points, markdown, or special symbols). "
                "Menu: Cheeseburger ($5), Veggie Burger ($4), Fries ($2), Coke ($2)."
            )
        }
    ]
    
    async def generate_bot_response(user_text: str):
        nonlocal is_bot_speaking, pending_audio_chunks
        is_bot_speaking = True
        chat_history.append({"role": "user", "content": user_text})
        audio_queue: asyncio.Queue = asyncio.Queue()

        async def stream_text():
            assistant_response = ""
            sentence_buffer = ""
            stream = await groq_client.chat.completions.create(
                model="openai/gpt-oss-20b",
                messages=chat_history,
                stream=True,
                temperature=0.7
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta.content
                if delta:
                    assistant_response += delta
                    sentence_buffer += delta
                    await websocket.send_text(f"ASSISTANT_CHUNK: {delta}")
                    if delta in ['.', '!', '?']:
                        sentence = sentence_buffer.strip()
                        if sentence:
                            await audio_queue.put(sentence)
                        sentence_buffer = ""
            if sentence_buffer.strip():
                await audio_queue.put(sentence_buffer.strip())
            await audio_queue.put(None)  # sentinel: text is done
            return assistant_response

        async def speak_sentences():
            nonlocal pending_audio_chunks
            while True:
                sentence = await audio_queue.get()
                if sentence is None:
                    break
                communicate = edge_tts.Communicate(sentence, "en-US-AvaNeural")
                sentence_audio = bytearray()
                async for tts_chunk in communicate.stream():
                    if tts_chunk["type"] == "audio":
                        sentence_audio.extend(tts_chunk["data"])
                if sentence_audio:
                    await websocket.send_bytes(bytes(sentence_audio))
                    pending_audio_chunks += 1  # client now has audio it hasn't finished playing

        text_task = asyncio.create_task(stream_text())
        audio_task = asyncio.create_task(speak_sentences())

        try:
            assistant_response, _ = await asyncio.gather(text_task, audio_task)
            print(f"🤖 Assistant said: {assistant_response}")
            await websocket.send_text("ASSISTANT_DONE")
            chat_history.append({"role": "assistant", "content": assistant_response})
        except asyncio.CancelledError:
            print("⚡ Generation cancelled (barge-in)")
            text_task.cancel()
            audio_task.cancel()
            await asyncio.gather(text_task, audio_task, return_exceptions=True)
        except Exception as e:
            print(f"⚠️ Error in bot response: {e}")
            await websocket.send_text("ASSISTANT_DONE")
        finally:
            # Only stop considering the bot "speaking" once nothing is left playing client-side
            if pending_audio_chunks == 0:
                is_bot_speaking = False

    try:
        while True:
            message = await websocket.receive()
            
            if message.get("type") == "websocket.disconnect":
                print("🔴 Client sent disconnect signal")
                break
                
            # Client confirms one queued audio chunk finished playing
            if message.get("text") == "AUDIO_ACK":
                pending_audio_chunks = max(0, pending_audio_chunks - 1)
                if pending_audio_chunks == 0 and current_response_task and current_response_task.done():
                    is_bot_speaking = False
                continue
                
            if "bytes" in message and message["bytes"]:
                audio_bytes = message["bytes"]
                audio_np = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                
                chunk_size = 512
                for i in range(0, len(audio_np) - chunk_size + 1, chunk_size):
                    sub_chunk = audio_np[i:i + chunk_size]
                    audio_tensor = torch.from_numpy(sub_chunk)
                    confidence = model(audio_tensor, 16000).item()
                    
                    if confidence > 0.5:
                        if is_bot_speaking:
                            # --- BARGE-IN TRIGGERED ---
                            # Don't gate this on current_response_task.done() — generation
                            # usually finishes well before playback does, so the task is
                            # often already done by the time the user interrupts.
                            print("⚡ User interrupted the bot! Stopping playback now...")
                            if current_response_task and not current_response_task.done():
                                current_response_task.cancel()
                            pending_audio_chunks = 0
                            is_bot_speaking = False
                            speech_buffer.clear()
                            is_speaking = True
                            await websocket.send_text("ASSISTANT_INTERRUPTED")
                            await websocket.send_text("STOP_AUDIO")
                            break
                        
                        if not is_speaking:
                            print("🗣️ Speech started!")
                            is_speaking = True
                        silence_chunks = 0
                    elif is_speaking:
                        silence_chunks += 1
                
                if is_bot_speaking:
                    continue
                
                speech_buffer.extend(audio_bytes)
                
                if is_speaking and silence_chunks >= SILENCE_THRESHOLD_CHUNKS:
                    print("🛑 Speech ended. Transcribing...")
                    is_speaking = False
                    silence_chunks = 0
                    
                    try:
                        wav_data = create_wav_buffer(bytes(speech_buffer))
                        transcription = await groq_client.audio.transcriptions.create(
                            file=("audio.wav", wav_data.read()),
                            model="whisper-large-v3",
                            response_format="text"
                        )
                        
                        clean_text = transcription.strip()
                        print(f"📝 User said: {clean_text}")
                        
                        if clean_text:
                            await websocket.send_text(f"USER: {clean_text}")
                            current_response_task = asyncio.create_task(generate_bot_response(clean_text))
                            
                    except Exception as e:
                        print(f"⚠️ Error processing transcription: {e}")
                    
                    speech_buffer.clear()

    except WebSocketDisconnect:
        print("🔴 Client disconnected")
    except Exception as e:
        print(f"⚠️ Unexpected socket error: {e}")