import sqlite3
import json


def init_database():
    conn = sqlite3.connect('trainer_app.db')
    cursor = conn.cursor()

    # Create users table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            business_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Create clients table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS clients (
            id TEXT PRIMARY KEY,
            trainer_id TEXT NOT NULL,
            name TEXT NOT NULL,
            email TEXT,
            phone TEXT,
            age INTEGER,
            gender TEXT,
            weight REAL,
            height REAL,
            status TEXT DEFAULT 'active',
            notes TEXT,
            photo_url TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (trainer_id) REFERENCES users (id)
        )
    ''')

    # Create sessions table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            trainer_id TEXT NOT NULL,
            client_id TEXT NOT NULL,
            session_date DATE NOT NULL,
            start_time TIME NOT NULL,
            end_time TIME NOT NULL,
            session_type TEXT DEFAULT 'training',
            status TEXT DEFAULT 'scheduled',
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP,
            FOREIGN KEY (trainer_id) REFERENCES users (id),
            FOREIGN KEY (client_id) REFERENCES clients (id)
        )
    ''')

    try:
        cursor.execute('ALTER TABLE sessions ADD COLUMN updated_at TIMESTAMP')
    except sqlite3.OperationalError:
        # Column already exists
        pass

    # Create workout_logs table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS workout_logs (
            id TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            trainer_id TEXT NOT NULL,
            exercise_name TEXT NOT NULL,
            sets INTEGER,
            reps INTEGER,
            weight REAL,
            notes TEXT,
            workout_date DATE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP,
            FOREIGN KEY (client_id) REFERENCES clients (id),
            FOREIGN KEY (trainer_id) REFERENCES users (id)
        )
    ''')

    try:
        cursor.execute('ALTER TABLE workout_logs ADD COLUMN sets_data TEXT')
    except sqlite3.OperationalError:
        # Column already exists
        pass

    # Create exercises table for autocomplete
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS exercises (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            muscle_group TEXT,
            equipment TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS weight_logs (
            id TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            date DATE NOT NULL,
            weight REAL NOT NULL,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP,
            FOREIGN KEY (client_id) REFERENCES clients (id),
            UNIQUE(client_id, date)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS client_notes (
            id TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            trainer_id TEXT NOT NULL,
            note_text TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (client_id) REFERENCES clients (id),
            FOREIGN KEY (trainer_id) REFERENCES users (id)
        )
    ''')

    # Insert sample exercises if table is empty
    cursor.execute('SELECT COUNT(*) FROM exercises')
    if cursor.fetchone()[0] == 0:
        sample_exercises = [
            ('Bench Press', 'Chest', 'Barbell'),
            ('Squat', 'Legs', 'Barbell'),
            ('Deadlift', 'Back', 'Barbell'),
            ('Pull-ups', 'Back', 'Bodyweight'),
            ('Push-ups', 'Chest', 'Bodyweight'),
            ('Shoulder Press', 'Shoulders', 'Dumbbell'),
            ('Bicep Curls', 'Arms', 'Dumbbell'),
            ('Tricep Dips', 'Arms', 'Bodyweight'),
            ('Lunges', 'Legs', 'Bodyweight'),
            ('Plank', 'Core', 'Bodyweight')
        ]

        cursor.executemany('''
            INSERT INTO exercises (name, muscle_group, equipment)
            VALUES (?, ?, ?)
        ''', sample_exercises)

    conn.commit()
    conn.close()
    print("Database initialized successfully!")


if __name__ == '__main__':
    init_database()
