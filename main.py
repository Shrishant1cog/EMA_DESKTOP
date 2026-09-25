import os
import threading
import uvicorn
from kivy.app import App
from kivy.uix.modalview import ModalView
from kivy.utils import platform

# Import your FastAPI app
from backend.main import app as fastapi_app

def run_server():
    # Use standard asyncio loop (uvloop does not compile easily on Android)
    uvicorn.run(fastapi_app, host="127.0.0.1", port=8000, log_level="info", loop="asyncio")

class MainApp(App):
    def build(self):
        # Start backend daemon
        server_thread = threading.Thread(target=run_server, daemon=True)
        server_thread.start()

        # Render Android WebView
        if platform == 'android':
            from jnius import autoclass
            from android.run_on_ui_thread import run_on_ui_thread

            WebView = autoclass('android.webkit.WebView')
            WebViewClient = autoclass('android.webkit.WebViewClient')
            activity = autoclass('org.kivy.android.PythonActivity').mActivity

            @run_on_ui_thread
            def create_webview():
                webview = WebView(activity)
                webview.getSettings().setJavaScriptEnabled(True)
                webview.getSettings().setDomStorageEnabled(True)
                webview.setWebViewClient(WebViewClient())
                # Load local backend serving frontend/index.html
                webview.loadUrl('http://127.0.0.1:8000')
                activity.setContentView(webview)

            create_webview()
        return ModalView()

if __name__ == '__main__':
    MainApp().run()