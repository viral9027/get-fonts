from flask import Blueprint, render_template, jsonify

main = Blueprint('main', __name__)

@main.route('/')
def home():
    return render_template('index.html')

@main.route('/api/data', methods=['GET'])
def get_data():
    return jsonify({'message': 'Hello from Flask API!'})