# server.py

from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route('/chat', methods=['POST'])
def chat():
    data = request.json
    message = data.get('message')
    return jsonify({"response": f"Ответ на сообщение: {message}"})

if __name__ == '__main__':
    app.run(debug=True)
