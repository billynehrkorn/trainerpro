from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import sqlite3
import os
from datetime import datetime, timedelta
import uuid
from functools import wraps
import json

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-change-this'
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

# Ensure upload directory exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)


def get_db():
    conn = sqlite3.connect('trainer_app.db')
    conn.row_factory = sqlite3.Row
    return conn


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)

    return decorated_function


@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']

        conn = get_db()
        user = conn.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
        conn.close()

        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            session['user_name'] = user['name']
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid email or password')

    return render_template('auth/login.html')


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        password = request.form['password']
        business_name = request.form.get('business_name', '')

        conn = get_db()
        existing_user = conn.execute('SELECT id FROM users WHERE email = ?', (email,)).fetchone()

        if existing_user:
            flash('Email already registered')
            conn.close()
            return render_template('auth/signup.html')

        user_id = str(uuid.uuid4())
        password_hash = generate_password_hash(password)

        conn.execute('''
            INSERT INTO users (id, name, email, password_hash, business_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_id, name, email, password_hash, business_name, datetime.now()))
        conn.commit()
        conn.close()

        flash('Account created successfully! Please log in.')
        return redirect(url_for('login'))

    return render_template('auth/signup.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/dashboard')
@login_required
def dashboard():
    conn = get_db()

    # Get client counts
    total_clients = \
        conn.execute('SELECT COUNT(*) as count FROM clients WHERE trainer_id = ?', (session['user_id'],)).fetchone()[
            'count']
    active_clients = conn.execute('SELECT COUNT(*) as count FROM clients WHERE trainer_id = ? AND status = "active"',
                                  (session['user_id'],)).fetchone()['count']

    # Get recent sessions
    recent_sessions = conn.execute('''
        SELECT s.*, c.name as client_name 
        FROM sessions s 
        JOIN clients c ON s.client_id = c.id 
        WHERE s.trainer_id = ? AND s.session_date >= date('now') 
        ORDER BY s.session_date, s.start_time 
        LIMIT 5
    ''', (session['user_id'],)).fetchall()

    conn.close()

    return render_template('dashboard/index.html',
                           total_clients=total_clients,
                           active_clients=active_clients,
                           recent_sessions=recent_sessions)


@app.route('/clients')
@login_required
def clients():
    search = request.args.get('search', '')
    status_filter = request.args.get('status', '')

    conn = get_db()
    query = 'SELECT * FROM clients WHERE trainer_id = ?'
    params = [session['user_id']]

    if search:
        query += ' AND (name LIKE ? OR email LIKE ?)'
        params.extend([f'%{search}%', f'%{search}%'])

    if status_filter:
        query += ' AND status = ?'
        params.append(status_filter)

    query += ' ORDER BY created_at DESC'

    clients = conn.execute(query, params).fetchall()
    conn.close()

    return render_template('dashboard/clients.html', clients=clients, search=search, status_filter=status_filter)


@app.route('/clients/new', methods=['GET', 'POST'])
@login_required
def new_client():
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        phone = request.form.get('phone', '')
        age = request.form.get('age', type=int)
        gender = request.form.get('gender', '')
        weight = request.form.get('weight', type=float)
        height = request.form.get('height', type=float)
        status = request.form['status']
        notes = request.form.get('notes', '')

        # Handle photo upload
        photo_url = None
        if 'photo' in request.files:
            file = request.files['photo']
            if file and file.filename:
                filename = secure_filename(f"{uuid.uuid4()}_{file.filename}")
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                photo_url = f"uploads/{filename}"

        client_id = str(uuid.uuid4())

        conn = get_db()
        conn.execute('''
            INSERT INTO clients (id, trainer_id, name, email, phone, age, gender, weight, height, status, notes, photo_url, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (client_id, session['user_id'], name, email, phone, age, gender, weight, height, status, notes, photo_url,
              datetime.now()))
        conn.commit()
        conn.close()

        flash('Client added successfully!')
        return redirect(url_for('clients'))

    return render_template('dashboard/clients/new.html')


@app.route('/clients/<client_id>')
@login_required
def client_detail(client_id):
    conn = get_db()
    client = conn.execute('SELECT * FROM clients WHERE id = ? AND trainer_id = ?',
                          (client_id, session['user_id'])).fetchone()

    if not client:
        flash('Client not found')
        return redirect(url_for('clients'))

    upcoming_sessions = conn.execute('''
        SELECT id, session_date, start_time, end_time, session_type, notes, status
        FROM sessions 
        WHERE client_id = ? AND session_date >= date('now') AND status != 'completed'
        ORDER BY session_date, start_time 
        LIMIT 5
    ''', (client_id,)).fetchall()

    # Get recent workouts
    recent_workouts = conn.execute('''
        SELECT workout_date, COUNT(*) as exercise_count
        FROM workout_logs 
        WHERE client_id = ? 
        GROUP BY workout_date 
        ORDER BY workout_date DESC 
        LIMIT 5
    ''', (client_id,)).fetchall()

    weight_history = conn.execute('''
        SELECT date, weight, notes 
        FROM weight_logs 
        WHERE client_id = ? 
        ORDER BY date DESC 
        LIMIT 10
    ''', (client_id,)).fetchall()

    client_notes = conn.execute('''
        SELECT id, note_text, created_at 
        FROM client_notes 
        WHERE client_id = ? 
        ORDER BY created_at DESC 
        LIMIT 5
    ''', (client_id,)).fetchall()

    conn.close()

    return render_template('dashboard/clients/detail.html',
                           client=client,
                           upcoming_sessions=upcoming_sessions,
                           recent_workouts=recent_workouts,
                           weight_history=weight_history,
                           client_notes=client_notes)


@app.route('/clients/<client_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_client(client_id):
    conn = get_db()
    client = conn.execute('SELECT * FROM clients WHERE id = ? AND trainer_id = ?',
                          (client_id, session['user_id'])).fetchone()

    if not client:
        flash('Client not found')
        return redirect(url_for('clients'))

    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        phone = request.form.get('phone', '')
        age = request.form.get('age', type=int)
        gender = request.form.get('gender', '')
        weight = request.form.get('weight', type=float)
        height = request.form.get('height', type=float)
        status = request.form['status']
        notes = request.form.get('notes', '')

        # Handle photo upload
        photo_url = client['photo_url']  # Keep existing photo by default
        if 'photo' in request.files:
            file = request.files['photo']
            if file and file.filename:
                # Delete old photo if it exists
                if photo_url and os.path.exists(os.path.join('static', photo_url)):
                    os.remove(os.path.join('static', photo_url))

                filename = secure_filename(f"{uuid.uuid4()}_{file.filename}")
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                photo_url = f"uploads/{filename}"

        conn.execute('''
            UPDATE clients 
            SET name = ?, email = ?, phone = ?, age = ?, gender = ?, weight = ?, height = ?, 
                status = ?, notes = ?, photo_url = ?
            WHERE id = ? AND trainer_id = ?
        ''', (name, email, phone, age, gender, weight, height, status, notes, photo_url,
              client_id, session['user_id']))
        conn.commit()
        conn.close()

        flash('Client updated successfully!')
        return redirect(url_for('client_detail', client_id=client_id))

    conn.close()
    return render_template('dashboard/clients/edit.html', client=client)


@app.route('/clients/<client_id>/workouts')
@login_required
def client_workouts(client_id):
    conn = get_db()
    client = conn.execute('SELECT * FROM clients WHERE id = ? AND trainer_id = ?',
                          (client_id, session['user_id'])).fetchone()

    if not client:
        flash('Client not found')
        return redirect(url_for('clients'))

    workouts = conn.execute('''
        SELECT workout_date, COUNT(*) as exercise_count
        FROM workout_logs 
        WHERE client_id = ? 
        GROUP BY workout_date 
        ORDER BY workout_date DESC
    ''', (client_id,)).fetchall()

    conn.close()

    return render_template('dashboard/clients/workouts.html', client=client, workouts=workouts)


@app.route('/clients/<client_id>/workouts/new', methods=['GET', 'POST'])
@login_required
def new_workout(client_id):
    conn = get_db()
    client = conn.execute('SELECT * FROM clients WHERE id = ? AND trainer_id = ?',
                          (client_id, session['user_id'])).fetchone()

    if not client:
        flash('Client not found')
        return redirect(url_for('clients'))

    if request.method == 'POST':
        workout_date = request.form['workout_date']
        exercises = request.form.getlist('exercise_name[]')
        notes_list = request.form.getlist('exercise_notes[]')

        for i, exercise in enumerate(exercises):
            if exercise.strip():  # Only save non-empty exercises
                # Get sets for this specific exercise using the new field naming
                exercise_weights = request.form.getlist(f'exercise_{i}_weight[]')
                exercise_reps = request.form.getlist(f'exercise_{i}_reps[]')

                # Build sets data for this exercise
                exercise_sets = []
                for j in range(len(exercise_weights)):
                    weight = exercise_weights[j] if j < len(exercise_weights) and exercise_weights[j] else None
                    reps = exercise_reps[j] if j < len(exercise_reps) and exercise_reps[j] else None

                    exercise_sets.append({
                        'weight': float(weight) if weight else None,
                        'reps': int(reps) if reps else None
                    })

                # If no sets data, create a default set
                if not exercise_sets:
                    exercise_sets = [{'weight': None, 'reps': None}]

                # Calculate totals for backward compatibility
                total_sets = len(exercise_sets)
                avg_reps = sum(s['reps'] for s in exercise_sets if s['reps']) // len(
                    [s for s in exercise_sets if s['reps']]) if any(s['reps'] for s in exercise_sets) else None
                avg_weight = sum(s['weight'] for s in exercise_sets if s['weight']) / len(
                    [s for s in exercise_sets if s['weight']]) if any(s['weight'] for s in exercise_sets) else None

                conn.execute('''
                    INSERT INTO workout_logs (id, client_id, trainer_id, exercise_name, sets, reps, weight, notes, workout_date, sets_data, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (str(uuid.uuid4()), client_id, session['user_id'], exercise.strip(),
                      total_sets, avg_reps, avg_weight,
                      notes_list[i] if i < len(notes_list) else '',
                      workout_date, json.dumps(exercise_sets), datetime.now()))

        conn.commit()
        conn.close()

        flash('Workout logged successfully!')
        return redirect(url_for('client_workouts', client_id=client_id))

    conn.close()
    return render_template('dashboard/clients/new_workout.html', client=client, today=datetime.now().date())


@app.route('/api/exercises/search')
@login_required
def search_exercises():
    query = request.args.get('q', '').lower()

    conn = get_db()
    exercises = conn.execute('''
        SELECT name, muscle_group, equipment 
        FROM exercises 
        WHERE LOWER(name) LIKE ? 
        ORDER BY name 
        LIMIT 10
    ''', (f'%{query}%',)).fetchall()
    conn.close()

    return jsonify([{
        'name': ex['name'],
        'muscle_group': ex['muscle_group'],
        'equipment': ex['equipment']
    } for ex in exercises])


@app.route('/calendar')
@login_required
def calendar():
    # Get current week with offset
    week_offset = request.args.get('week_offset', 0, type=int)

    today = datetime.now().date()
    start_of_week = today - timedelta(days=today.weekday()) + timedelta(weeks=week_offset)
    end_of_week = start_of_week + timedelta(days=6)

    week_dates = []
    for i in range(7):
        week_dates.append(start_of_week + timedelta(days=i))

    user_id = session['user_id']

    conn = get_db()
    sessions_data = conn.execute('''
        SELECT s.*, c.name as client_name 
        FROM sessions s 
        JOIN clients c ON s.client_id = c.id 
        WHERE s.trainer_id = ? AND s.session_date BETWEEN ? AND ?
        ORDER BY s.session_date, s.start_time
    ''', (user_id, start_of_week, end_of_week)).fetchall()

    clients = conn.execute('SELECT id, name FROM clients WHERE trainer_id = ? ORDER BY name', (user_id,)).fetchall()

    conn.close()

    # Organize sessions by day
    week_sessions = {}
    for i in range(7):
        day = start_of_week + timedelta(days=i)
        week_sessions[day.strftime('%Y-%m-%d')] = []

    for session_item in sessions_data:
        day_key = session_item['session_date']
        if day_key in week_sessions:
            week_sessions[day_key].append(session_item)

    return render_template('dashboard/calendar.html',
                           week_sessions=week_sessions,
                           week_dates=week_dates,
                           week_start=start_of_week,
                           week_end=end_of_week,
                           week_offset=week_offset,
                           clients=clients)


@app.route('/api/sessions', methods=['POST'])
@login_required
def create_session():
    data = request.json
    client_id = data['client_id']
    session_date = data['session_date']
    start_time = data['start_time']
    end_time = data['end_time']
    session_type = data.get('session_type', 'training')
    notes = data.get('notes', '')

    session_id = str(uuid.uuid4())

    user_id = session['user_id']

    conn = get_db()
    conn.execute('''
        INSERT INTO sessions (id, trainer_id, client_id, session_date, start_time, end_time, session_type, status, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'scheduled', ?, ?)
    ''', (session_id, user_id, client_id, session_date, start_time, end_time, session_type, notes, datetime.now()))
    conn.commit()
    conn.close()

    return jsonify({'success': True, 'session_id': session_id})


@app.route('/api/sessions/<session_id>', methods=['GET'])
@login_required
def get_session(session_id):
    conn = get_db()
    session_data = conn.execute('''
        SELECT * FROM sessions 
        WHERE id = ? AND trainer_id = ?
    ''', (session_id, session['user_id'])).fetchone()

    if not session_data:
        conn.close()
        return jsonify({'error': 'Session not found'}), 404

    conn.close()
    return jsonify(dict(session_data))


@app.route('/api/sessions/<session_id>', methods=['PUT'])
@login_required
def update_session(session_id):
    data = request.json

    conn = get_db()
    # Verify session belongs to current trainer
    session_data = conn.execute('''
        SELECT * FROM sessions 
        WHERE id = ? AND trainer_id = ?
    ''', (session_id, session['user_id'])).fetchone()

    if not session_data:
        conn.close()
        return jsonify({'error': 'Session not found'}), 404

    conn.execute('''
        UPDATE sessions 
        SET session_date = ?, start_time = ?, end_time = ?, session_type = ?, 
            notes = ?, status = ?, updated_at = ?
        WHERE id = ? AND trainer_id = ?
    ''', (data['session_date'], data['start_time'], data['end_time'],
          data['session_type'], data.get('notes', ''),
          data.get('status', 'scheduled'), datetime.now(),
          session_id, session['user_id']))

    conn.commit()
    conn.close()

    return jsonify({'success': True})


@app.route('/api/sessions/<session_id>', methods=['DELETE'])
@login_required
def delete_session(session_id):
    conn = get_db()
    # Verify session belongs to current trainer
    session_data = conn.execute('''
        SELECT * FROM sessions 
        WHERE id = ? AND trainer_id = ?
    ''', (session_id, session['user_id'])).fetchone()

    if not session_data:
        conn.close()
        return jsonify({'error': 'Session not found'}), 404

    conn.execute('DELETE FROM sessions WHERE id = ? AND trainer_id = ?',
                 (session_id, session['user_id']))

    conn.commit()
    conn.close()

    return jsonify({'success': True})


@app.route('/api/sessions/<session_id>/complete', methods=['POST'])
@login_required
def complete_session(session_id):
    conn = get_db()
    # Verify session belongs to current trainer
    session_data = conn.execute('''
        SELECT * FROM sessions 
        WHERE id = ? AND trainer_id = ?
    ''', (session_id, session['user_id'])).fetchone()

    if not session_data:
        conn.close()
        return jsonify({'error': 'Session not found'}), 404

    conn.execute('''
        UPDATE sessions 
        SET status = 'completed', updated_at = ?
        WHERE id = ? AND trainer_id = ?
    ''', (datetime.now(), session_id, session['user_id']))

    conn.commit()
    conn.close()

    return jsonify({'success': True})


@app.route('/api/sessions/<session_id>/cancel', methods=['POST'])
@login_required
def cancel_session(session_id):
    conn = get_db()
    # Verify session belongs to current trainer
    session_data = conn.execute('''
        SELECT * FROM sessions 
        WHERE id = ? AND trainer_id = ?
    ''', (session_id, session['user_id'])).fetchone()

    if not session_data:
        conn.close()
        return jsonify({'error': 'Session not found'}), 404

    conn.execute('''
        UPDATE sessions 
        SET status = 'cancelled', updated_at = ?
        WHERE id = ? AND trainer_id = ?
    ''', (datetime.now(), session_id, session['user_id']))

    conn.commit()
    conn.close()

    return jsonify({'success': True})


@app.route('/clients/<client_id>/sessions/history')
@login_required
def session_history(client_id):
    conn = get_db()
    client = conn.execute('SELECT * FROM clients WHERE id = ? AND trainer_id = ?',
                          (client_id, session['user_id'])).fetchone()

    if not client:
        flash('Client not found')
        return redirect(url_for('clients'))

    all_sessions = conn.execute('''
        SELECT * FROM sessions 
        WHERE client_id = ? AND trainer_id = ?
        ORDER BY session_date DESC, start_time DESC
    ''', (client_id, session['user_id'])).fetchall()

    conn.close()

    return render_template('dashboard/clients/session_history.html',
                           client=client, all_sessions=all_sessions)


@app.route('/clients/<client_id>/workouts/<date>')
@login_required
def workout_detail(client_id, date):
    conn = get_db()
    client = conn.execute('SELECT * FROM clients WHERE id = ? AND trainer_id = ?',
                          (client_id, session['user_id'])).fetchone()

    if not client:
        flash('Client not found')
        return redirect(url_for('clients'))

    exercises = conn.execute('''
        SELECT id, exercise_name, sets, reps, weight, notes, sets_data 
        FROM workout_logs 
        WHERE client_id = ? AND workout_date = ? AND trainer_id = ?
        ORDER BY created_at
    ''', (client_id, date, session['user_id'])).fetchall()

    conn.close()

    result = []
    for ex in exercises:
        sets_data = None
        if ex['sets_data']:
            try:
                sets_data = json.loads(ex['sets_data'])
            except:
                sets_data = None

        result.append({
            'id': ex['id'],
            'exercise_name': ex['exercise_name'],
            'sets': ex['sets'],
            'reps': ex['reps'],
            'weight': ex['weight'],
            'notes': ex['notes'],
            'sets_data': sets_data
        })

    return jsonify(result)


@app.route('/api/update-workout/<client_id>/<date>', methods=['POST'])
@login_required
def update_workout(client_id, date):
    conn = get_db()
    # Verify client belongs to current trainer
    client = conn.execute('SELECT * FROM clients WHERE id = ? AND trainer_id = ?',
                          (client_id, session['user_id'])).fetchone()

    if not client:
        conn.close()
        return jsonify({'error': 'Client not found'}), 404

    exercise_names = request.form.getlist('exercise_name[]')
    notes_list = request.form.getlist('exercise_notes[]')

    # Delete existing exercises for this workout date
    conn.execute('''
        DELETE FROM workout_logs 
        WHERE client_id = ? AND workout_date = ? AND trainer_id = ?
    ''', (client_id, date, session['user_id']))

    for i, exercise_name in enumerate(exercise_names):
        if exercise_name.strip():  # Only save non-empty exercises
            # Get sets for this specific exercise using the new field naming
            exercise_weights = request.form.getlist(f'exercise_{i}_weight[]')
            exercise_reps = request.form.getlist(f'exercise_{i}_reps[]')

            # Build sets data for this exercise
            exercise_sets = []
            for j in range(len(exercise_weights)):
                weight = exercise_weights[j] if j < len(exercise_weights) and exercise_weights[j] else None
                reps = exercise_reps[j] if j < len(exercise_reps) and exercise_reps[j] else None

                exercise_sets.append({
                    'weight': float(weight) if weight else None,
                    'reps': int(reps) if reps else None
                })

            # If no sets data, create a default set
            if not exercise_sets:
                exercise_sets = [{'weight': None, 'reps': None}]

            # Calculate totals for backward compatibility
            total_sets = len(exercise_sets)
            avg_reps = sum(s['reps'] for s in exercise_sets if s['reps']) // len(
                [s for s in exercise_sets if s['reps']]) if any(s['reps'] for s in exercise_sets) else None
            avg_weight = sum(s['weight'] for s in exercise_sets if s['weight']) / len(
                [s for s in exercise_sets if s['weight']]) if any(s['weight'] for s in exercise_sets) else None

            notes_val = notes_list[i] if i < len(notes_list) else ''

            conn.execute('''
                INSERT INTO workout_logs (id, client_id, trainer_id, exercise_name, sets, reps, weight, notes, workout_date, sets_data, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (str(uuid.uuid4()), client_id, session['user_id'], exercise_name.strip(),
                  total_sets, avg_reps, avg_weight, notes_val, date, json.dumps(exercise_sets), datetime.now()))

    conn.commit()
    conn.close()

    return jsonify({'success': True})


@app.route('/api/delete-workout/<client_id>/<date>', methods=['DELETE'])
@login_required
def delete_workout(client_id, date):
    conn = get_db()
    # Verify client belongs to current trainer
    client = conn.execute('SELECT * FROM clients WHERE id = ? AND trainer_id = ?',
                          (client_id, session['user_id'])).fetchone()

    if not client:
        conn.close()
        return jsonify({'error': 'Client not found'}), 404

    # Delete all exercises for this workout date
    conn.execute('''
        DELETE FROM workout_logs 
        WHERE client_id = ? AND workout_date = ? AND trainer_id = ?
    ''', (client_id, date, session['user_id']))

    conn.commit()
    conn.close()

    return jsonify({'success': True})


@app.route('/api/weight-log', methods=['POST'])
@login_required
def add_weight_log():
    data = request.json
    client_id = data['client_id']
    date = data['date']
    weight = data['weight']
    notes = data.get('notes', '')

    # Verify client belongs to current trainer
    conn = get_db()
    client = conn.execute('SELECT * FROM clients WHERE id = ? AND trainer_id = ?',
                          (client_id, session['user_id'])).fetchone()

    if not client:
        conn.close()
        return jsonify({'error': 'Client not found'}), 404

    weight_id = str(uuid.uuid4())

    try:
        conn.execute('''
            INSERT OR REPLACE INTO weight_logs (id, client_id, date, weight, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (weight_id, client_id, date, weight, notes, datetime.now(), datetime.now()))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500


@app.route('/api/client-notes', methods=['POST'])
@login_required
def add_client_note():
    data = request.json
    client_id = data['client_id']
    note_text = data['note_text']

    # Verify client belongs to current trainer
    conn = get_db()
    client = conn.execute('SELECT * FROM clients WHERE id = ? AND trainer_id = ?',
                          (client_id, session['user_id'])).fetchone()

    if not client:
        conn.close()
        return jsonify({'error': 'Client not found'}), 404

    note_id = str(uuid.uuid4())

    try:
        conn.execute('''
            INSERT INTO client_notes (id, client_id, trainer_id, note_text, created_at)
            VALUES (?, ?, ?, ?, ?)
        ''', (note_id, client_id, session['user_id'], note_text, datetime.now()))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500


@app.route('/api/upload-client-photo', methods=['POST'])
@login_required
def upload_client_photo():
    client_id = request.form['client_id']

    # Verify client belongs to current trainer
    conn = get_db()
    client = conn.execute('SELECT * FROM clients WHERE id = ? AND trainer_id = ?',
                          (client_id, session['user_id'])).fetchone()

    if not client:
        conn.close()
        return jsonify({'error': 'Client not found'}), 404

    if 'photo' not in request.files:
        conn.close()
        return jsonify({'error': 'No photo provided'}), 400

    file = request.files['photo']
    if file and file.filename:
        # Delete old photo if it exists
        if client['photo_url'] and os.path.exists(os.path.join('static', client['photo_url'])):
            os.remove(os.path.join('static', client['photo_url']))

        filename = secure_filename(f"{uuid.uuid4()}_{file.filename}")
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        photo_url = f"uploads/{filename}"

        conn.execute('''
            UPDATE clients SET photo_url = ? WHERE id = ? AND trainer_id = ?
        ''', (photo_url, client_id, session['user_id']))
        conn.commit()
        conn.close()
        return jsonify({'success': True})

    conn.close()
    return jsonify({'error': 'Invalid file'}), 400


@app.route('/api/workout-details/<client_id>/<date>')
@login_required
def api_workout_details(client_id, date):
    conn = get_db()
    client = conn.execute('SELECT * FROM clients WHERE id = ? AND trainer_id = ?',
                          (client_id, session['user_id'])).fetchone()

    if not client:
        conn.close()
        return jsonify({'error': 'Client not found'}), 404

    exercises = conn.execute('''
        SELECT id, exercise_name, sets, reps, weight, notes, sets_data 
        FROM workout_logs 
        WHERE client_id = ? AND workout_date = ? AND trainer_id = ?
        ORDER BY created_at
    ''', (client_id, date, session['user_id'])).fetchall()

    conn.close()

    result = []
    for ex in exercises:
        sets_data = None
        if ex['sets_data']:
            try:
                sets_data = json.loads(ex['sets_data'])
            except:
                sets_data = None

        result.append({
            'id': ex['id'],
            'exercise_name': ex['exercise_name'],
            'sets': ex['sets'],
            'reps': ex['reps'],
            'weight': ex['weight'],
            'notes': ex['notes'],
            'sets_data': sets_data
        })

    return jsonify(result)


@app.route('/clients/<client_id>/delete', methods=['POST'])
@login_required
def delete_client(client_id):
    conn = get_db()
    client = conn.execute('SELECT * FROM clients WHERE id = ? AND trainer_id = ?',
                          (client_id, session['user_id'])).fetchone()

    if not client:
        conn.close()
        return jsonify({'error': 'Client not found'}), 404

    try:
        # Delete client photo if it exists
        if client['photo_url'] and os.path.exists(os.path.join('static', client['photo_url'])):
            os.remove(os.path.join('static', client['photo_url']))

        # Delete all related data
        conn.execute('DELETE FROM sessions WHERE client_id = ? AND trainer_id = ?',
                     (client_id, session['user_id']))
        conn.execute('DELETE FROM workout_logs WHERE client_id = ? AND trainer_id = ?',
                     (client_id, session['user_id']))
        conn.execute('DELETE FROM weight_logs WHERE client_id = ?', (client_id,))
        conn.execute('DELETE FROM client_notes WHERE client_id = ? AND trainer_id = ?',
                     (client_id, session['user_id']))

        # Finally delete the client
        conn.execute('DELETE FROM clients WHERE id = ? AND trainer_id = ?',
                     (client_id, session['user_id']))

        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    from init_db import init_database

    init_database()
    app.run(debug=True)
