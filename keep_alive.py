import os
from threading import Thread
from flask import Flask

app = Flask(__name__)

@app.route('/')
def home():
    return 'TSL Opinion Manager is running'

def run():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), use_reloader=False)

def keep_alive():
    Thread(target=run, daemon=True).start()
