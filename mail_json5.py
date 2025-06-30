from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_login import LoginManager, login_user, login_required, logout_user, current_user, UserMixin
import bcrypt
import asyncio
import threading
import json
import websockets
import eventlet
import eventlet.wsgi

app = Flask(__name__)
app.secret_key = 'supersecretkey123'
last_telemetry = {}
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

DATA_FILE = 'data.json'

# Чтение и запись данных
def load_data():
    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# Пользователь для Flask-Login
class User(UserMixin):
    def __init__(self, id_, username):
        self.id = id_
        self.username = username

@login_manager.user_loader
def load_user(user_id):
    data = load_data()
    for u in data['users']:
        if str(u['id']) == user_id:
            return User(u['id'], u['username'])
    return None

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password'].encode('utf-8')

        data = load_data()
        for u in data['users']:
            if u['username'] == username and bcrypt.checkpw(password, u['password_hash'].encode('utf-8')):
                user = User(u['id'], username)
                login_user(user)
                return redirect(url_for('index'))
        flash("Неверный логин или пароль")
        return redirect(url_for('login'))

    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    data = load_data()
    devices = [(d['name'], d['status']) for d in data['devices']]
    return render_template('index.html', devices=devices, username=current_user.username)

authenticated_clients = {}

async def ws_handler(websocket):
    name = None
    try:
        message = await websocket.recv()
        print(f"message {message}")
        data = json.loads(message)
        if data.get('type') != 'auth':
            await websocket.send(json.dumps({"error": "auth_required"}))
            return

        name = data.get('name')
        password = data.get('password')

        all_data = load_data()
        device = next((d for d in all_data['devices'] if d['name'] == name), None)

        if not device or device['password'] != password:
            await websocket.send(json.dumps({"error": "unauthorized"}))
            return

        print(f"✅ Устройство {name} подключилось")
        authenticated_clients[name] = websocket

        # Обновим статус в JSON
        device['status'] = 'online'
        save_data(all_data)

        async for message in websocket:
            data = json.loads(message)
            if data.get('type') == 'telemetry':
                status = data.get('status', 'unknown')
                print(f"📡 Телеметрия от {name}: {status}")
                device['status'] = status
                last_telemetry[name] = data 
                save_data(all_data)

    except websockets.exceptions.ConnectionClosed:
        print(f"❌ {name} отключился")
    finally:
        if name:
            authenticated_clients.pop(name, None)
            all_data = load_data()
            device = next((d for d in all_data['devices'] if d['name'] == name), None)
            if device:
                device['status'] = 'offline'
                save_data(all_data)

# Новый маршрут для получения телеметрии
@app.route('/get_telemetry')
@login_required
def get_telemetry():
    device_name = request.args.get('device')
    if device_name in last_telemetry:
        return jsonify(last_telemetry[device_name])
    return jsonify({"error": "no data"})

async def send_command_to(name, command, value=None):  # Делаем value необязательным
    ws = authenticated_clients.get(name)
    if not ws:
        return False
    
    # Формируем команду в зависимости от наличия value
    if value is not None:
        cmd_json = json.dumps({
            "command": command,
            "value": str(value)
        })
    else:
        cmd_json = json.dumps({
            "command": command
        })
    
    await ws.send(cmd_json)
    return True
@app.route('/send_command', methods=['POST'])
@login_required
def send_command():
    name = request.form.get('device_name')
    command = request.form.get('command')
    value = request.form.get('value', None)  # Безопасное получение value
    
    if not name or not command:
        flash("Заполните обязательные поля")
        return redirect(url_for('index'))
    
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        success = loop.run_until_complete(send_command_to(name, command, value))
        loop.close()
        
        if success:
            flash(f"Команда {command} отправлена")
        else:
            flash("Устройство offline")
    except Exception as e:
        flash(f"Ошибка: {str(e)}")
    
    return redirect(url_for('index'))



def start_websocket_server():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    async def server():
        async with websockets.serve(ws_handler, '0.0.0.0', 8765):
            await asyncio.Future()  # run forever
    
    loop.run_until_complete(server())

if __name__ == '__main__':
    # Запуск WebSocket сервера в отдельном потоке
    t = threading.Thread(target=start_websocket_server, daemon=True)
    t.start()
    
    # Запуск Flask сервера
    eventlet.wsgi.server(eventlet.listen(('', 5000)), app)
