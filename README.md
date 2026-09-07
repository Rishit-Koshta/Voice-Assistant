# 🎙️ Real-Time Interruptible Voice Assistant

A real-time, bidirectional voice assistant built with **FastAPI, WebSockets, Silero VAD, Groq Whisper, Llama 3, and edge-tts**. It features instant **barge-in interruption capabilities**—allowing the user to cut off the AI mid-sentence.

## Tech Stack
- **Backend:** Python, FastAPI, WebSockets, PyTorch (Silero VAD), edge-tts
- **AI Models:** Groq Whisper (STT) & Groq Llama 3 (LLM Brain)
- **Frontend:** Vanilla HTML/JS, Web Audio API

## How to Run Locally
1. Clone the repository and navigate to the folder.
2. Create and activate a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
