from flask import Flask, render_template, request, redirect, url_for
import re
import uuid
import sqlite3
import json

from flask import Response
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.pagesizes import letter

from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user

app = Flask(__name__)
app.secret_key = "super_secret_key"

# ---------------- LOGIN SETUP ----------------
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

# ---------------- DATABASE INIT ----------------
def init_db():
    conn = sqlite3.connect("quiz.db")
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password TEXT
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS quizzes (
        id TEXT PRIMARY KEY,
        owner_id INTEGER,
        questions TEXT,
        answers TEXT
    )
    """)

    # 🔥 ADD THIS BLOCK
    c.execute("""
    CREATE TABLE IF NOT EXISTS attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        quiz_id TEXT,
        name TEXT,
        score INTEGER,
        total INTEGER,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)

    conn.commit()
    conn.close()

# ---------------- USER MODEL ----------------
class User(UserMixin):
    def __init__(self, id, username, password):
        self.id = str(id)
        self.username = username
        self.password = password

@login_manager.user_loader
def load_user(user_id):
    conn = sqlite3.connect("quiz.db")
    c = conn.cursor()

    c.execute("SELECT id, username, password FROM users WHERE id=?", (user_id,))
    row = c.fetchone()
    conn.close()

    if row:
        return User(row[0], row[1], row[2])
    return None

# ---------------- PARSERS ----------------
def parse_mcqs(text):
    questions = []
    blocks = re.split(r'\n(?=\d+\.)', text.strip())

    for block in blocks:
        lines = block.strip().split("\n")
        q_text = re.sub(r'^\d+\.\s*', '', lines[0])

        options = {}
        for line in lines[1:]:
            match = re.match(r'\s*([A-D])\)\s*(.*)', line)
            if match:
                options[match.group(1)] = match.group(2)

        questions.append({
            "question": q_text,
            "options": options
        })

    return questions

def parse_answers(text):
    answers = {}
    for line in text.strip().split("\n"):
        match = re.match(r'(\d+)\.\s*([A-D])', line.strip())
        if match:
            answers[str(match.group(1))] = match.group(2)
    return answers

# ---------------- AUTH ----------------
@app.route('/')
def home():
    if current_user.is_authenticated:
        return redirect('/dashboard')
    return render_template('home.html')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form.get("username")
        password = request.form.get("password")

        conn = sqlite3.connect("quiz.db")
        c = conn.cursor()

        try:
            c.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, password))
            conn.commit()

            user_id = c.lastrowid
            user = User(user_id, username, password)
            login_user(user)

            return redirect('/dashboard')
        except:
            return "Username already exists"
        finally:
            conn.close()

    return render_template("signup.html")

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get("username")
        password = request.form.get("password")

        conn = sqlite3.connect("quiz.db")
        c = conn.cursor()

        c.execute("SELECT id, username, password FROM users WHERE username=?", (username,))
        row = c.fetchone()
        conn.close()

        if row and row[2] == password:
            user = User(row[0], row[1], row[2])
            login_user(user)
            return redirect('/dashboard')

        return "Invalid credentials"

    return render_template("login.html")

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect('/login')

# ---------------- DASHBOARD ----------------
@app.route('/dashboard')
@login_required
def dashboard():
    conn = sqlite3.connect("quiz.db")
    c = conn.cursor()

    c.execute("SELECT id FROM quizzes WHERE owner_id=?", (current_user.id,))
    rows = c.fetchall()
    conn.close()

    quizzes = [row[0] for row in rows]

    return render_template("dashboard.html", quizzes=quizzes)

# ---------------- CREATE QUIZ ----------------
@app.route('/create_quiz', methods=['GET', 'POST'])
@login_required
def create_quiz():
    if request.method == 'POST':
        q_file = request.files.get('questions_file')
        a_file = request.files.get('answers_file')

        questions = []
        answers = {}

        if q_file:
            text = q_file.read().decode('utf-8')
            questions = parse_mcqs(text)

        if a_file:
            ans_text = a_file.read().decode('utf-8')
            answers = parse_answers(ans_text)

        quiz_id = str(uuid.uuid4())[:8]

        conn = sqlite3.connect("quiz.db")
        c = conn.cursor()

        c.execute("""
        INSERT INTO quizzes (id, owner_id, questions, answers)
        VALUES (?, ?, ?, ?)
        """, (
            quiz_id,
            current_user.id,
            json.dumps(questions),
            json.dumps(answers)
        ))

        conn.commit()
        conn.close()

        return redirect('/dashboard')

    return render_template("create_quiz.html")

# ---------------- EDIT QUIZ ----------------
@app.route('/edit_quiz/<quiz_id>', methods=['GET', 'POST'])
@login_required
def edit_quiz(quiz_id):
    conn = sqlite3.connect("quiz.db")
    c = conn.cursor()

    c.execute("SELECT owner_id FROM quizzes WHERE id=?", (quiz_id,))
    row = c.fetchone()

    if not row or str(row[0]) != current_user.id:
        conn.close()
        return "Unauthorized"

    if request.method == 'POST':
        q_file = request.files.get('questions_file')
        a_file = request.files.get('answers_file')

        if q_file:
            text = q_file.read().decode('utf-8')
            questions = parse_mcqs(text)
            c.execute("UPDATE quizzes SET questions=? WHERE id=?", (json.dumps(questions), quiz_id))

        if a_file:
            ans_text = a_file.read().decode('utf-8')
            answers = parse_answers(ans_text)
            c.execute("UPDATE quizzes SET answers=? WHERE id=?", (json.dumps(answers), quiz_id))

        conn.commit()
        conn.close()

        return redirect('/dashboard')

    conn.close()
    return render_template("edit_quiz.html", quiz_id=quiz_id)

# ---------------- TAKE QUIZ ----------------
@app.route('/quiz/<quiz_id>')
def take_quiz(quiz_id):
    conn = sqlite3.connect("quiz.db")
    c = conn.cursor()

    c.execute("SELECT questions FROM quizzes WHERE id=?", (quiz_id,))
    row = c.fetchone()
    conn.close()

    if not row:
        return "Quiz not found"

    questions = json.loads(row[0])

    return render_template("quiz.html", questions=questions, quiz_id=quiz_id)

# ---------------- SUBMIT ----------------
@app.route('/submit/<quiz_id>', methods=['POST'])
def submit(quiz_id):
    # ---------------- FETCH QUIZ ----------------
    conn = sqlite3.connect("quiz.db")
    c = conn.cursor()

    c.execute("SELECT questions, answers FROM quizzes WHERE id=?", (quiz_id,))
    row = c.fetchone()
    conn.close()

    if not row:
        return "Quiz not found"

    questions = json.loads(row[0])
    answers = json.loads(row[1])

    # ---------------- INIT ----------------
    score = 0
    results = []
    name = request.form.get("name")

    # ---------------- EVALUATE ----------------
    for i, q in enumerate(questions, start=1):
        user_ans = request.form.get(f"q{i}")
        correct_ans = answers.get(str(i))

        is_correct = user_ans == correct_ans
        if is_correct:
            score += 1

        results.append({
            "question": q["question"],
            "options": q["options"],
            "your": user_ans if user_ans else "Not Answered",
            "correct": correct_ans,
            "is_correct": is_correct
        })

    # ---------------- SAVE ATTEMPT (ONLY ONCE) ----------------
    conn = sqlite3.connect("quiz.db")
    c = conn.cursor()

    c.execute("""
    INSERT INTO attempts (quiz_id, name, score, total)
    VALUES (?, ?, ?, ?)
    """, (quiz_id, name, score, len(questions)))

    conn.commit()
    conn.close()

    # ---------------- RETURN RESULT ----------------
    return render_template(
        "result.html",
        score=score,
        total=len(questions),
        results=results,
        name=name,
        quiz_id=quiz_id
    )


@app.route('/results/<quiz_id>')
@login_required
def view_results(quiz_id):
    conn = sqlite3.connect("quiz.db")
    c = conn.cursor()

    c.execute("""
    SELECT name, score, total, timestamp
    FROM attempts
    WHERE quiz_id=?
    ORDER BY score DESC
    """, (quiz_id,))

    results = c.fetchall()
    conn.close()

    return render_template("view_results.html", results=results, quiz_id=quiz_id)


@app.route('/download_report/<quiz_id>')
@login_required
def download_report(quiz_id):
    import csv
    from flask import Response

    conn = sqlite3.connect("quiz.db")
    c = conn.cursor()

    c.execute("""
    SELECT name, score, total, timestamp
    FROM attempts
    WHERE quiz_id=?
    """, (quiz_id,))

    rows = c.fetchall()
    conn.close()

    def generate():
        yield "Name,Score,Total,Timestamp\n"
        for row in rows:
            yield f"{row[0]},{row[1]},{row[2]},{row[3]}\n"

    return Response(generate(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment;filename=report_{quiz_id}.csv"})

@app.route('/download_result/<quiz_id>', methods=['POST'])
def download_result(quiz_id):
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet
    import io

    conn = sqlite3.connect("quiz.db")
    c = conn.cursor()

    c.execute("SELECT questions, answers FROM quizzes WHERE id=?", (quiz_id,))
    row = c.fetchone()
    conn.close()

    if not row:
        return "Quiz not found"

    questions = json.loads(row[0])
    answers = json.loads(row[1])

    name = request.form.get("name")

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)

    styles = getSampleStyleSheet()
    content = []

    score = 0

    content.append(Paragraph(f"Result for: {name}", styles['Title']))
    content.append(Spacer(1, 10))

    for i, q in enumerate(questions, start=1):
        user_ans = request.form.get(f"q{i}")
        correct_ans = answers.get(str(i))

        is_correct = user_ans == correct_ans
        if is_correct:
            score += 1

        content.append(Paragraph(f"{i}. {q['question']}", styles['Normal']))
        content.append(Spacer(1, 5))

        content.append(Paragraph(f"Your Answer: {user_ans if user_ans else 'Not Answered'}", styles['Normal']))
        content.append(Paragraph(f"Correct Answer: {correct_ans}", styles['Normal']))

        result_text = "Correct ✅" if is_correct else "Wrong ❌"
        content.append(Paragraph(result_text, styles['Normal']))

        content.append(Spacer(1, 10))

    content.insert(1, Paragraph(f"Score: {score}/{len(questions)}", styles['Normal']))

    doc.build(content)

    buffer.seek(0)

    return Response(
        buffer,
        mimetype='application/pdf',
        headers={"Content-Disposition": f"attachment;filename=result_{quiz_id}.pdf"}
    )

# ---------------- RUN ----------------
if __name__ == '__main__':
    init_db()
    app.run(debug=True)