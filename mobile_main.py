import os
import threading
import time
import uvicorn
from kivy.app import App
from kivy.uix.boxlayout import BoxLayout

# Import your FastAPI app
from backend.main import app as fastapi_app

def start_server():
    uvicorn.run(fastapi_app, host="127.0.0.1", port=8000, log_level="warning")

class EMAApp(App):
    def build(self):
        # 1. Start FastAPI server on a background daemon thread
        server_thread = threading.Thread(target=start_server, daemon=True)
        server_thread.start()

        # Wait briefly for local socket to bind
        time.sleep(1.5)

        # 2. Launch Android WebView full-screen pointing to localhost
        from jnius import autoclass
        from android.runnable import run_on_ui_thread

        # Uses native Android WebView wrapper layout
        layout = BoxLayout(orientation='vertical')
        
        # Load local web interface
        self.load_webview("http://127.0.0.1:8000")
        return layout

    def load_webview(self, url):
        # Platform-specific WebView initialization
        try:
            from plyer import webview
            webview.open(url)
        except Exception:
            import webbrowser
            webbrowser.open(url)

if __name__ == "__main__":
    EMAApp().run()