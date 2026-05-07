from flask import Flask, render_template, request, redirect, url_for
import re
import uuid
import difflib

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
        c.execute("ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS quiz_type TEXT DEFAULT 'mcq'")
        c.execute("ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS has_answer_key BOOLEAN DEFAULT TRUE")
        c.execute("ALTER TABLE attempts ADD COLUMN IF NOT EXISTS responses TEXT DEFAULT '{}'")
        c.execute("ALTER TABLE attempts ADD COLUMN IF NOT EXISTS manual_score DOUBLE PRECISION DEFAULT NULL")
        conn.commit()
    except Exception as e:
        print(f"Database migration error: {e}")
    finally:
        conn.close()


def ensure_quiz_columns():
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS quiz_name TEXT")
        c.execute("ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS quiz_type TEXT DEFAULT 'mcq'")
        c.execute("ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS has_answer_key BOOLEAN DEFAULT TRUE")
        c.execute("ALTER TABLE attempts ADD COLUMN IF NOT EXISTS responses TEXT DEFAULT '{}'")
        c.execute("ALTER TABLE attempts ADD COLUMN IF NOT EXISTS manual_score DOUBLE PRECISION DEFAULT NULL")
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


def normalize_text(value):
    if not isinstance(value, str):
        return ""
    value = value.strip().lower()
    value = re.sub(r'[\W_]+', ' ', value)
    return re.sub(r'\s+', ' ', value).strip()


def is_written_answer_correct(user_answer, correct_answer):
    if not user_answer or not correct_answer:
        return False

    user = normalize_text(user_answer)
    correct = normalize_text(correct_answer)

    if user == correct:
        return True
    if correct in user or user in correct:
        return True

    user_tokens = user.split()
    correct_tokens = correct.split()
    if not correct_tokens:
        return False

    bad_answer_phrases = {
        'idk', 'dont know', 'do not know', 'no idea', 'not sure',
        'i think', 'i guess', 'maybe', 'perhaps', 'hehe', 'haha', 'hmm'
    }
    joined_user = ' '.join(user_tokens)
    if len(user_tokens) <= 4 and any(phrase in joined_user for phrase in bad_answer_phrases):
        return False

    user_set = set(user_tokens)
    correct_set = set(correct_tokens)
    common_tokens = user_set & correct_set
    token_ratio = len(common_tokens) / len(correct_tokens)
    if token_ratio >= 0.75:
        return True

    stopwords = {
        'the', 'a', 'an', 'and', 'or', 'of', 'in', 'on', 'to', 'for',
        'with', 'as', 'by', 'at', 'from', 'about', 'into', 'over',
        'after', 'before', 'between', 'is', 'are', 'was', 'were', 'be',
        'it', 'this', 'that', 'these', 'those'
    }

    significant_correct = [t for t in correct_tokens if t not in stopwords]
    significant_user = [t for t in user_tokens if t not in stopwords]
    if significant_correct:
        significant_match = len(set(significant_correct) & set(significant_user)) / len(set(significant_correct))
        if significant_match >= 0.7:
            # require more substance when the answer is short
            return len(user_tokens) >= 3

    similarity = difflib.SequenceMatcher(None, user, correct).ratio()
    if similarity >= 0.80:
        return True

    if len(user_tokens) < 5:
        return False

    if token_ratio >= 0.60 and similarity >= 0.70:
        return True

    return False


def parse_mcqs(text):
    questions = []
    answers = {}
    blocks = re.split(r'\n(?=\d+\.)', text.strip())

    for block in blocks:
        lines = block.strip().split("\n")
        q_text = re.sub(r'^\d+\.\s*', '', lines[0])

        options = {}
        inline_answer = None
        for line in lines[1:]:
            answer_match = re.match(r'ANSWER:\s*([A-D])', line.strip(), re.IGNORECASE)
            if answer_match:
                inline_answer = answer_match.group(1).upper()
                continue

            match = re.match(r'\s*([A-D])\)\s*(.*)', line)
            if match:
                options[match.group(1)] = match.group(2).strip()

        questions.append({
            "type": "mcq",
            "question": q_text,
            "options": options
        })
        if inline_answer:
            answers[str(len(questions))] = inline_answer

    return questions, answers


def parse_written_quiz(text):
    questions = []
    answers = {}
    for i, line in enumerate(text.strip().split("\n"), start=1):
        if not line.strip():
            continue
        if '|' in line:
            q_text, answer_text = line.split('|', 1)
        elif ':' in line and line.count(':') == 1:
            q_text, answer_text = line.split(':', 1)
        elif '=' in line and line.count('=') == 1:
            q_text, answer_text = line.split('=', 1)
        else:
            continue
        q_text = q_text.strip()
        answer_text = answer_text.strip()
        if q_text and answer_text:
            questions.append({
                "type": "written",
                "question": q_text
            })
            answers[str(len(questions))] = answer_text
    return questions, answers


def parse_match_quiz(text):
    pairs = []
    for line in text.strip().split("\n"):
        if not line.strip():
            continue
        if '->' in line:
            left, right = line.split('->', 1)
        elif ':' in line and line.count(':') == 1:
            left, right = line.split(':', 1)
        elif '=' in line and line.count('=') == 1:
            left, right = line.split('=', 1)
        elif '-' in line and line.count('-') == 1:
            left, right = line.split('-', 1)
        else:
            continue
        pairs.append({
            "left": left.strip(),
            "right": right.strip()
        })

    questions = []
    answers = {}
    if pairs:
        for idx, pair in enumerate(pairs, start=1):
            pair['id'] = str(idx)
        questions.append({
            "type": "match",
            "question": "Match the columns",
            "pairs": pairs
        })
        answers["1"] = {str(idx): str(idx) for idx in range(1, len(pairs)+1)}
    return questions, answers


def parse_mix_quiz(text):
    questions = []
    answers = {}
    blocks = re.split(r'\n\s*\n', text.strip())
    for block in blocks:
        if not block.strip():
            continue
        lines = block.strip().split("\n")
        mcq_lines = [line for line in lines if re.match(r'\s*[A-D]\)', line)]
        answer_line = None
        for line in lines:
            match = re.match(r'ANSWER:\s*(.*)', line.strip(), re.IGNORECASE)
            if match:
                answer_line = match.group(1).strip()
                break

        if mcq_lines:
            block_questions, block_answers = parse_mcqs(block)
            idx_offset = len(questions)
            for q in block_questions:
                questions.append(q)
            for key, value in block_answers.items():
                answers[str(idx_offset + int(key))] = value
            continue

        if '|' in block or (':' in block and len(lines) == 1) or ('=' in block and len(lines) == 1):
            block_questions, block_answers = parse_written_quiz(block)
            idx_offset = len(questions)
            for q in block_questions:
                questions.append(q)
            for key, value in block_answers.items():
                answers[str(idx_offset + int(key))] = value
            continue

        match_pairs = []
        for line in lines:
            if '->' in line:
                left, right = line.split('->', 1)
            elif ':' in line and line.count(':') == 1:
                left, right = line.split(':', 1)
            elif '=' in line and line.count('=') == 1:
                left, right = line.split('=', 1)
            elif '-' in line and line.count('-') == 1:
                left, right = line.split('-', 1)
            else:
                continue
            match_pairs.append({"left": left.strip(), "right": right.strip()})

        if match_pairs:
            for idx, pair in enumerate(match_pairs, start=1):
                pair['id'] = str(idx)
            questions.append({
                "type": "match",
                "question": "Match the columns",
                "pairs": match_pairs
            })
            answers[str(len(questions))] = {str(idx): str(idx) for idx in range(1, len(match_pairs)+1)}
            continue

    return questions, answers

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
        c.execute("SELECT id, quiz_name, quiz_type, has_answer_key FROM quizzes WHERE owner=%s", (current_user.id,))
    except psycopg2.errors.UndefinedColumn:
        conn.close()
        ensure_quiz_columns()
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id, quiz_name, quiz_type, has_answer_key FROM quizzes WHERE owner=%s", (current_user.id,))

    rows = c.fetchall()
    conn.close()

    quizzes = [
        {
            "id": row[0],
            "name": row[1] if row[1] else 'Untitled Quiz',
            "type": row[2] if row[2] else 'mcq',
            "has_answer_key": row[3] if row[3] is not None else True
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
        quiz_type = request.form.get('quiz_type', 'mcq')

        questions = []
        answers = {}
        has_answer_key = False  # Default to manual checking

        if q_file:
            text = extract_text_from_file(q_file, q_file.filename)
            if quiz_type == 'written':
                questions, answers = parse_written_quiz(text)
                has_answer_key = True if answers else False  # Has answer key if answers were extracted
            elif quiz_type == 'match':
                questions, answers = parse_match_quiz(text)
                has_answer_key = True if answers else False
            elif quiz_type == 'mix':
                questions, answers = parse_mix_quiz(text)
                has_answer_key = True if answers else False
            else:
                questions, answers = parse_mcqs(text)
                has_answer_key = True if answers else False  # MCQ may include inline answer key

        if a_file and quiz_type == 'mcq':
            ans_text = extract_text_from_file(a_file, a_file.filename)
            file_answers = parse_answers(ans_text)
            answers.update(file_answers)
            has_answer_key = True

        quiz_id = str(uuid.uuid4())[:8]

        conn = get_db()
        c = conn.cursor()

        c.execute("""
        INSERT INTO quizzes (id, owner, questions, answers, teacher_message, quiz_name, quiz_type, has_answer_key)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            quiz_id,
            current_user.id,
            json.dumps(questions),
            json.dumps(answers),
            teacher_message,
            quiz_name,
            quiz_type,
            has_answer_key
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
        quiz_type = request.form.get('quiz_type', 'mcq')
        has_answer_key = False

        if q_file:
            text = extract_text_from_file(q_file, q_file.filename)
            if quiz_type == 'written':
                questions, answers = parse_written_quiz(text)
                has_answer_key = True if answers else False
            elif quiz_type == 'match':
                questions, answers = parse_match_quiz(text)
                has_answer_key = True if answers else False
            elif quiz_type == 'mix':
                questions, answers = parse_mix_quiz(text)
                has_answer_key = True if answers else False
            else:
                questions, answers = parse_mcqs(text)
                has_answer_key = True if answers else False
            c.execute("UPDATE quizzes SET questions=%s, answers=%s, has_answer_key=%s WHERE id=%s", (json.dumps(questions), json.dumps(answers), has_answer_key, quiz_id))

        if a_file and quiz_type == 'mcq':
            ans_text = extract_text_from_file(a_file, a_file.filename)
            answers = parse_answers(ans_text)
            has_answer_key = True
            c.execute("UPDATE quizzes SET answers=%s, has_answer_key=%s WHERE id=%s", (json.dumps(answers), has_answer_key, quiz_id))

        c.execute("UPDATE quizzes SET quiz_name=%s, teacher_message=%s, quiz_type=%s WHERE id=%s", (quiz_name, teacher_message, quiz_type, quiz_id))

        conn.commit()
        conn.close()

        return redirect('/dashboard')

    # Get current quiz_name, teacher_message and type for the form
    c.execute("SELECT quiz_name, teacher_message, quiz_type FROM quizzes WHERE id=%s", (quiz_id,))
    row = c.fetchone()
    quiz_name = row[0] if row else ''
    teacher_message = row[1] if row else ''
    quiz_type = row[2] if row else 'mcq'

    conn.close()
    return render_template("edit_quiz.html", quiz_id=quiz_id, quiz_name=quiz_name, teacher_message=teacher_message, quiz_type=quiz_type)

# ---------------- TAKE QUIZ ----------------
@app.route('/quiz/<quiz_id>')
def take_quiz(quiz_id):
    conn = get_db()
    c = conn.cursor()

    try:
        c.execute("SELECT questions, teacher_message, quiz_name, quiz_type FROM quizzes WHERE id=%s", (quiz_id,))
    except psycopg2.errors.UndefinedColumn:
        conn.close()
        ensure_quiz_columns()
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT questions, teacher_message, quiz_name, quiz_type FROM quizzes WHERE id=%s", (quiz_id,))

    row = c.fetchone()
    conn.close()

    if not row:
        return "Quiz not found"

    questions = json.loads(row[0])
    teacher_message = row[1] or ''
    quiz_name = row[2] or ''
    quiz_type = row[3] or 'mcq'

    return render_template("quiz.html", questions=questions, quiz_id=quiz_id, teacher_message=teacher_message, quiz_name=quiz_name, quiz_type=quiz_type)


def evaluate_submission(questions, answers, form):
    score = 0
    total = 0
    results = []

    for i, q in enumerate(questions, start=1):
        q_type = q.get('type', 'mcq')

        if q_type == 'mcq':
            user_ans = form.get(f"q{i}")
            correct_ans = answers.get(str(i)) or q.get('answer') or ''
            is_correct = user_ans == correct_ans
            if is_correct:
                score += 1
            total += 1
            results.append({
                "type": "mcq",
                "question": q.get('question'),
                "options": q.get('options', {}),
                "your": user_ans if user_ans else "Not Answered",
                "correct": correct_ans,
                "is_correct": is_correct
            })

        elif q_type == 'written':
            user_ans = form.get(f"q{i}", "").strip()
            correct_ans = answers.get(str(i)) or q.get('answer') or ''
            is_correct = is_written_answer_correct(user_ans, correct_ans)
            if is_correct:
                score += 1
            total += 1
            results.append({
                "type": "written",
                "question": q.get('question'),
                "your": user_ans if user_ans else "Not Answered",
                "correct": correct_ans,
                "is_correct": is_correct
            })

        elif q_type == 'match':
            pairs = q.get('pairs', [])
            pair_results = []
            correct_count = 0
            for idx, pair in enumerate(pairs, start=1):
                user_value = form.get(f"q{i}_{idx}")
                selected_text = "Not Answered"
                selected_index = ""
                if user_value and user_value.isdigit():
                    selected_index = user_value
                    numeric_index = int(user_value) - 1
                    if 0 <= numeric_index < len(pairs):
                        selected_text = pairs[numeric_index].get('right', selected_text)
                expected_text = pair.get('right')
                is_pair_correct = user_value == str(idx)
                if is_pair_correct:
                    correct_count += 1
                pair_results.append({
                    "left": pair.get('left'),
                    "right": expected_text,
                    "selected": selected_text,
                    "selected_index": selected_index,
                    "expected_index": str(idx),
                    "is_correct": is_pair_correct
                })
            score += correct_count
            total += len(pairs)
            results.append({
                "type": "match",
                "question": q.get('question'),
                "pairs": pairs,
                "pair_results": pair_results,
                "correct_count": correct_count,
                "total_pairs": len(pairs),
                "is_correct": correct_count == len(pairs)
            })

        else:
            user_ans = form.get(f"q{i}", "").strip()
            correct_ans = answers.get(str(i)) or q.get('answer') or ''
            is_correct = is_written_answer_correct(user_ans, correct_ans)
            if is_correct:
                score += 1
            total += 1
            results.append({
                "type": "written",
                "question": q.get('question'),
                "your": user_ans if user_ans else "Not Answered",
                "correct": correct_ans,
                "is_correct": is_correct
            })

    return score, total, results

# ---------------- SUBMIT ----------------
@app.route('/submit/<quiz_id>', methods=['POST'])
def submit(quiz_id):
    # ---------------- FETCH QUIZ ----------------
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT questions, answers, has_answer_key FROM quizzes WHERE id=%s", (quiz_id,))
    row = c.fetchone()
    conn.close()

    if not row:
        return "Quiz not found"

    questions = json.loads(row[0])
    answers = json.loads(row[1])
    has_answer_key = row[2]

    name = request.form.get("name")
    
    # Collect all responses
    responses = {}
    for i, q in enumerate(questions, start=1):
        q_type = q.get('type', 'mcq')
        if q_type == 'match':
            responses[str(i)] = {str(j): request.form.get(f"q{i}_{j}") for j in range(1, len(q.get('pairs', [])) + 1)}
        else:
            responses[str(i)] = request.form.get(f"q{i}", "").strip()

    score = 0
    total = 0
    results = []

    if has_answer_key:
        # Auto-grade if answer key exists
        score, total, results = evaluate_submission(questions, answers, request.form)
    else:
        # Manual checking - don't auto-grade, just collect responses
        for i, q in enumerate(questions, start=1):
            q_type = q.get('type', 'mcq')
            
            if q_type == 'mcq':
                user_ans = request.form.get(f"q{i}", "")
                results.append({
                    "type": "mcq",
                    "question": q.get('question'),
                    "options": q.get('options', {}),
                    "your": user_ans if user_ans else "Not Answered",
                })
            elif q_type == 'written':
                user_ans = request.form.get(f"q{i}", "").strip()
                results.append({
                    "type": "written",
                    "question": q.get('question'),
                    "your": user_ans if user_ans else "Not Answered",
                })
            elif q_type == 'match':
                pairs = q.get('pairs', [])
                pair_results = []
                for idx, pair in enumerate(pairs, start=1):
                    user_value = request.form.get(f"q{i}_{idx}")
                    selected_text = "Not Answered"
                    if user_value and user_value.isdigit():
                        numeric_index = int(user_value) - 1
                        if 0 <= numeric_index < len(pairs):
                            selected_text = pairs[numeric_index].get('right', selected_text)
                    pair_results.append({
                        "left": pair.get('left'),
                        "selected": selected_text,
                    })
                results.append({
                    "type": "match",
                    "question": q.get('question'),
                    "pairs": pairs,
                    "pair_results": pair_results,
                })
            else:
                user_ans = request.form.get(f"q{i}", "").strip()
                results.append({
                    "type": "written",
                    "question": q.get('question'),
                    "your": user_ans if user_ans else "Not Answered",
                })
            total += 1

    # ---------------- SAVE ATTEMPT (ONLY ONCE) ----------------
    conn = get_db()
    c = conn.cursor()

    manual_score = score if has_answer_key else None
    
    c.execute("""
    INSERT INTO attempts (quiz_id, name, score, total, responses, manual_score)
    VALUES (%s, %s, %s, %s, %s, %s)
    """, (quiz_id, name, score if has_answer_key else 0, total, json.dumps(responses), manual_score))

    conn.commit()
    conn.close()

    # ---------------- RETURN RESULT ----------------
    return render_template(
        "result.html",
        score=score,
        total=total,
        results=results,
        name=name,
        quiz_id=quiz_id,
        has_answer_key=has_answer_key
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


# -------- MANUAL CHECKING --------
@app.route('/manual_check/<quiz_id>')
@login_required
def manual_check(quiz_id):
    """View all responses for manual grading"""
    conn = get_db()
    c = conn.cursor()

    # Check if user owns this quiz
    c.execute("SELECT owner, questions, quiz_name, quiz_type FROM quizzes WHERE id=%s", (quiz_id,))
    quiz_row = c.fetchone()
    
    if not quiz_row or str(quiz_row[0]) != current_user.id:
        conn.close()
        return "Unauthorized"

    questions = json.loads(quiz_row[1])
    quiz_name = quiz_row[2] or "Quiz"
    quiz_type = quiz_row[3] or "mcq"

    # Fetch all attempts with responses
    c.execute("""
    SELECT id, name, manual_score, responses, timestamp
    FROM attempts
    WHERE quiz_id=%s
    ORDER BY timestamp DESC
    """, (quiz_id,))

    attempts = c.fetchall()
    conn.close()

    attempts_data = []
    for attempt in attempts:
        attempt_id, name, manual_score, responses_json, timestamp = attempt
        try:
            responses = json.loads(responses_json) if responses_json else {}
        except:
            responses = {}

        # Build attempt details
        attempt_details = {
            "id": attempt_id,
            "name": name,
            "manual_score": manual_score,
            "timestamp": timestamp,
            "questions": []
        }

        for i, q in enumerate(questions, start=1):
            q_type = q.get('type', 'mcq')
            user_response = responses.get(str(i), "")
            
            q_data = {
                "number": i,
                "type": q_type,
                "question": q.get('question'),
                "response": user_response,
            }

            if q_type == 'mcq':
                q_data["options"] = q.get('options', {})
            elif q_type == 'match':
                q_data["pairs"] = q.get('pairs', [])
                if isinstance(user_response, dict):
                    q_data["response"] = user_response

            attempt_details["questions"].append(q_data)

        attempts_data.append(attempt_details)

    return render_template("manual_check.html", 
                         quiz_id=quiz_id, 
                         quiz_name=quiz_name,
                         quiz_type=quiz_type,
                         attempts=attempts_data,
                         total_questions=len(questions))


@app.route('/grade_attempt/<attempt_id>', methods=['POST'])
@login_required
def grade_attempt(attempt_id):
    """Save manual grades for an attempt"""
    conn = get_db()
    c = conn.cursor()

    # Get the attempt
    c.execute("SELECT quiz_id FROM attempts WHERE id=%s", (attempt_id,))
    attempt_row = c.fetchone()
    
    if not attempt_row:
        conn.close()
        return "Attempt not found", 404

    quiz_id = attempt_row[0]

    # Verify user owns the quiz
    c.execute("SELECT owner FROM quizzes WHERE id=%s", (quiz_id,))
    quiz_row = c.fetchone()
    
    if not quiz_row or str(quiz_row[0]) != current_user.id:
        conn.close()
        return "Unauthorized", 403

    # Get the grades from the request
    data = request.get_json()
    scores = data.get("scores", {})  # {question_id: score}
    
    # Calculate total score, supporting decimal values such as 0.5
    total_score = 0.0
    for score in scores.values():
        try:
            total_score += float(score)
        except (TypeError, ValueError):
            continue

    # Update the attempt with manual score
    c.execute("""
    UPDATE attempts 
    SET manual_score = %s
    WHERE id = %s
    """, (total_score, attempt_id))

    conn.commit()
    conn.close()

    return {"success": True, "score": total_score}



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

    score, total, results = evaluate_submission(questions, answers, request.form)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)

    styles = getSampleStyleSheet()
    content = []

    content.append(Paragraph(f"Result for: {name}", styles['Title']))
    content.append(Spacer(1, 10))
    content.append(Paragraph(f"Score: {score}/{total}", styles['Normal']))
    content.append(Spacer(1, 10))

    for i, result in enumerate(results, start=1):
        content.append(Paragraph(f"{i}. {result['question']}", styles['Normal']))
        content.append(Spacer(1, 5))

        if result['type'] == 'mcq':
            content.append(Paragraph(f"Your Answer: {result['your']}", styles['Normal']))
            content.append(Paragraph(f"Correct Answer: {result['correct']}", styles['Normal']))
        elif result['type'] == 'written':
            content.append(Paragraph(f"Your Answer: {result['your']}", styles['Normal']))
            content.append(Paragraph(f"Correct Answer: {result['correct']}", styles['Normal']))
        elif result['type'] == 'match':
            for pair_result in result['pair_results']:
                content.append(Paragraph(f"{pair_result['left']} → Your: {pair_result['selected']} | Correct: {pair_result.get('right', '')}", styles['Normal']))
        else:
            content.append(Paragraph(f"Your Answer: {result['your']}", styles['Normal']))
            content.append(Paragraph(f"Correct Answer: {result['correct']}", styles['Normal']))

        result_text = "Correct ✅" if result['is_correct'] else "Wrong ❌"
        content.append(Paragraph(result_text, styles['Normal']))
        content.append(Spacer(1, 10))

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