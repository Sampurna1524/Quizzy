from flask import Flask, render_template, request, redirect, url_for
import re
import uuid

import json
import psycopg2
import os

from PyPDF2 import PdfReader
from docx import Document

def get_db():
    return psycopg2.connect(os.environ.get("DATABASE_URL"))

# Database migration
def init_db():
    if not os.environ.get("DATABASE_URL"):
        return  # Skip if no database URL
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS teacher_message TEXT")
        c.execute("ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS quiz_name TEXT")
        conn.commit()
    except Exception as e:
        print(f"Database migration error: {e}")
    finally:
        conn.close()


def ensure_quiz_name_column():
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS quiz_name TEXT")
        conn.commit()
    except Exception as e:
        print(f"Database migration error: {e}")
    finally:
        conn.close()

# Run migration on startup
init_db()


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

# ---------------- USER MODEL ----------------
class User(UserMixin):
    def __init__(self, id, username, password):
        self.id = str(id)
        self.username = username
        self.password = password

@login_manager.user_loader
def load_user(user_id):
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT id, username, password FROM users WHERE id=%s", (user_id,))
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

# -------- PDF EXTRACTION --------
def extract_text_from_pdf(file_obj):
    try:
        pdf_reader = PdfReader(file_obj)
        text = ""
        for page in pdf_reader.pages:
            text += page.extract_text() + "\n"
        return text
    except Exception as e:
        print(f"PDF extraction error: {e}")
        return ""

# -------- WORD EXTRACTION --------
def extract_text_from_word(file_obj):
    try:
        doc = Document(file_obj)
        text = ""
        for paragraph in doc.paragraphs:
            text += paragraph.text + "\n"
        return text
    except Exception as e:
        print(f"Word extraction error: {e}")
        return ""

# -------- FILE TEXT EXTRACTION --------
def extract_text_from_file(file_obj, filename):
    """Extract text from .txt, .pdf, or .docx files"""
    if filename.lower().endswith('.pdf'):
        return extract_text_from_pdf(file_obj)
    elif filename.lower().endswith(('.docx', '.doc')):
        return extract_text_from_word(file_obj)
    else:  # Default to .txt
        try:
            file_obj.seek(0)
            return file_obj.read().decode('utf-8')
        except Exception as e:
            print(f"Text extraction error: {e}")
            return ""

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

        conn = get_db()
        c = conn.cursor()

        try:
            # 🔹 Check if username already exists
            c.execute("SELECT id FROM users WHERE username=%s", (username,))
            existing = c.fetchone()

            if existing:
                return "Username already exists"

            # 🔹 Generate unique user ID
            user_id = str(uuid.uuid4())

            # 🔹 Insert new user
            c.execute(
                "INSERT INTO users (id, username, password) VALUES (%s, %s, %s)",
                (user_id, username, password)
            )
            conn.commit()

            # 🔹 Login user immediately
            user = User(user_id, username, password)
            login_user(user)

            return redirect('/dashboard')

        except Exception as e:
            print("SIGNUP ERROR:", e)
            return f"Signup error: {str(e)}"

        finally:
            conn.close()

    return render_template("signup.html")

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get("username")
        password = request.form.get("password")

        conn = get_db()
        c = conn.cursor()

        c.execute("SELECT id, username, password FROM users WHERE username=%s", (username,))
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
    conn = get_db()
    c = conn.cursor()

    try:
        c.execute("SELECT id, quiz_name FROM quizzes WHERE owner=%s", (current_user.id,))
    except psycopg2.errors.UndefinedColumn:
        conn.close()
        ensure_quiz_name_column()
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id, quiz_name FROM quizzes WHERE owner=%s", (current_user.id,))

    rows = c.fetchall()
    conn.close()

    quizzes = [
        {
            "id": row[0],
            "name": row[1] if row[1] else 'Untitled Quiz'
        }
        for row in rows
    ]

    return render_template("dashboard.html", quizzes=quizzes)

# ---------------- DELETE QUIZ ----------------
@app.route('/delete_quiz/<quiz_id>', methods=['POST'])
@login_required
def delete_quiz(quiz_id):
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT owner FROM quizzes WHERE id=%s", (quiz_id,))
    row = c.fetchone()

    if not row or str(row[0]) != current_user.id:
        conn.close()
        return "Unauthorized"

    c.execute("DELETE FROM quizzes WHERE id=%s", (quiz_id,))
    conn.commit()
    conn.close()

    return redirect('/dashboard')

# ---------------- CREATE QUIZ ----------------
@app.route('/create_quiz', methods=['GET', 'POST'])
@login_required
def create_quiz():
    if request.method == 'POST':
        q_file = request.files.get('questions_file')
        a_file = request.files.get('answers_file')
        quiz_name = request.form.get('quiz_name', '').strip()
        teacher_message = request.form.get('teacher_message', '').strip()

        questions = []
        answers = {}

        if q_file:
            text = extract_text_from_file(q_file, q_file.filename)
            questions = parse_mcqs(text)

        if a_file:
            ans_text = extract_text_from_file(a_file, a_file.filename)
            answers = parse_answers(ans_text)

        quiz_id = str(uuid.uuid4())[:8]

        conn = get_db()
        c = conn.cursor()

        c.execute("""
        INSERT INTO quizzes (id, owner, questions, answers, teacher_message, quiz_name)
        VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            quiz_id,
            current_user.id,
            json.dumps(questions),
            json.dumps(answers),
            teacher_message,
            quiz_name
        ))

        conn.commit()
        conn.close()

        return redirect('/dashboard')

    return render_template("create_quiz.html")

# ---------------- EDIT QUIZ ----------------
@app.route('/edit_quiz/<quiz_id>', methods=['GET', 'POST'])
@login_required
def edit_quiz(quiz_id):
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT owner FROM quizzes WHERE id=%s", (quiz_id,))
    row = c.fetchone()

    if not row or str(row[0]) != current_user.id:
        conn.close()
        return "Unauthorized"

    if request.method == 'POST':
        q_file = request.files.get('questions_file')
        a_file = request.files.get('answers_file')
        quiz_name = request.form.get('quiz_name', '').strip()
        teacher_message = request.form.get('teacher_message', '').strip()

        if q_file:
            text = extract_text_from_file(q_file, q_file.filename)
            questions = parse_mcqs(text)
            c.execute("UPDATE quizzes SET questions=%s WHERE id=%s", (json.dumps(questions), quiz_id))

        if a_file:
            ans_text = extract_text_from_file(a_file, a_file.filename)
            answers = parse_answers(ans_text)
            c.execute("UPDATE quizzes SET answers=%s WHERE id=%s", (json.dumps(answers), quiz_id))

        c.execute("UPDATE quizzes SET quiz_name=%s, teacher_message=%s WHERE id=%s", (quiz_name, teacher_message, quiz_id))

        conn.commit()
        conn.close()

        return redirect('/dashboard')

    # Get current quiz_name and teacher_message for the form
    c.execute("SELECT quiz_name, teacher_message FROM quizzes WHERE id=%s", (quiz_id,))
    row = c.fetchone()
    quiz_name = row[0] if row else ''
    teacher_message = row[1] if row else ''

    conn.close()
    return render_template("edit_quiz.html", quiz_id=quiz_id, quiz_name=quiz_name, teacher_message=teacher_message)

# ---------------- TAKE QUIZ ----------------
@app.route('/quiz/<quiz_id>')
def take_quiz(quiz_id):
    conn = get_db()
    c = conn.cursor()

    try:
        c.execute("SELECT questions, teacher_message, quiz_name FROM quizzes WHERE id=%s", (quiz_id,))
    except psycopg2.errors.UndefinedColumn:
        conn.close()
        ensure_quiz_name_column()
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT questions, teacher_message, quiz_name FROM quizzes WHERE id=%s", (quiz_id,))

    row = c.fetchone()
    conn.close()

    if not row:
        return "Quiz not found"

    questions = json.loads(row[0])
    teacher_message = row[1] or ''
    quiz_name = row[2] or ''

    return render_template("quiz.html", questions=questions, quiz_id=quiz_id, teacher_message=teacher_message, quiz_name=quiz_name)

# ---------------- SUBMIT ----------------
@app.route('/submit/<quiz_id>', methods=['POST'])
def submit(quiz_id):
    # ---------------- FETCH QUIZ ----------------
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT questions, answers FROM quizzes WHERE id=%s", (quiz_id,))
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
    conn = get_db()
    c = conn.cursor()

    c.execute("""
    INSERT INTO attempts (quiz_id, name, score, total)
    VALUES (%s, %s, %s, %s)
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
    conn = get_db()
    c = conn.cursor()

    c.execute("""
    SELECT name, score, total, timestamp
    FROM attempts
    WHERE quiz_id=%s
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

    conn = get_db()
    c = conn.cursor()

    c.execute("""
    SELECT name, score, total, timestamp
    FROM attempts
    WHERE quiz_id=%s
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

    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT questions, answers FROM quizzes WHERE id=%s", (quiz_id,))
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
    app.run(debug=True)