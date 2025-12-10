import os
import json
import requests
import sys
from datetime import datetime, timedelta
from icalendar import Calendar, Event
import pytz

# --- CONFIGURATION & AUTH ---

def load_config():
    """Load configuration from environment variables or config.json"""
    if all(key in os.environ for key in ['WEBUNTIS_SERVER', 'WEBUNTIS_SCHOOL', 'WEBUNTIS_USERNAME', 'WEBUNTIS_PASSWORD']):
        return {
            'server': os.environ['WEBUNTIS_SERVER'],
            'school': os.environ['WEBUNTIS_SCHOOL'],
            'username': os.environ['WEBUNTIS_USERNAME'],
            'password': os.environ['WEBUNTIS_PASSWORD'],
            'class_id': os.environ.get('WEBUNTIS_CLASS_ID')
        }
    
    if os.path.exists('config.json'):
        with open('config.json', 'r') as f:
            return json.load(f)
    return {}

def webuntis_login(config):
    """Authenticate against WebUntis and return session + sessionId"""
    session = requests.Session()
    login_url = f"https://{config['server']}/WebUntis/jsonrpc.do?school={config['school']}"
    
    login_data = {
        "id": "WebUntisSync",
        "method": "authenticate",
        "params": {
            "user": config['username'],
            "password": config['password'],
            "client": "WebUntisSync"
        },
        "jsonrpc": "2.0"
    }
    
    try:
        response = session.post(login_url, json=login_data)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise Exception(f"Connection failed: {e}")
    
    result = response.json()
    if 'error' in result:
        raise Exception(f"Login failed: {result['error']}")
    
    return session, result['result']['sessionId']

def get_element_id(session, config, session_id):
    """Get element ID (class or student)."""
    if config.get('class_id'):
        print(f"📚 Using configured class ID: {config['class_id']}")
        return int(config['class_id']), 1
    
    url = f"https://{config['server']}/WebUntis/jsonrpc.do?school={config['school']}"
    headers = {"Cookie": f"JSESSIONID={session_id}"}
    
    # Try fetching classes
    data = {"id": "WebUntisSync", "method": "getKlassen", "params": {}, "jsonrpc": "2.0"}
    response = session.post(url, json=data, headers=headers)
    result = response.json()
    
    if 'result' in result and len(result['result']) > 0:
        first_class = result['result'][0]
        print(f"📚 Found class: {first_class['name']} (ID: {first_class['id']})")
        return first_class['id'], 1
    
    # Try fetching student
    data = {"id": "WebUntisSync", "method": "getStudents", "params": {}, "jsonrpc": "2.0"}
    response = session.post(url, json=data, headers=headers)
    result = response.json()
    
    if 'result' in result and len(result['result']) > 0:
        student = result['result'][0]
        print(f"👤 Found student: {student.get('name', 'Unknown')} (ID: {student['id']})")
        return student['id'], 5
    
    raise Exception("Could not find any Class or Student ID.")

# --- TIMETABLE FETCHING ---

def get_timetable(session, config, session_id, element_id, element_type, start_date, end_date):
    """Fetch timetable data from WebUntis in chunks"""
    full_timetable = []
    chunk_size = 28 # 4 weeks per chunk
    current_start = start_date
    
    print(f"🔄 Fetching timetable in chunks from {start_date} to {end_date}...")

    while current_start < end_date:
        current_end = min(current_start + timedelta(days=chunk_size), end_date)
        
        url = f"https://{config['server']}/WebUntis/jsonrpc.do?school={config['school']}"
        data = {
            "id": "WebUntisSync",
            "method": "getTimetable",
            "params": {
                "options": {
                    "element": {"id": element_id, "type": element_type},
                    "startDate": current_start.strftime("%Y%m%d"),
                    "endDate": current_end.strftime("%Y%m%d"),
                    "showBooking": True, "showInfo": True, 
                    "showSubstText": True, "showLsText": True, 
                    "showStudentgroup": True,
                    "klasseFields": ["id", "name", "longname"],
                    "roomFields": ["id", "name", "longname"],
                    "subjectFields": ["id", "name", "longname"],
                    "teacherFields": ["id", "name", "longname"]
                }
            },
            "jsonrpc": "2.0"
        }
        
        headers = {"Cookie": f"JSESSIONID={session_id}"}
        
        try:
            response = session.post(url, json=data, headers=headers)
            result = response.json()
            
            if 'error' in result:
                print(f"   ⚠️ Error fetching chunk {current_start}: {result['error']['message']}")
            else:
                items = result.get('result', [])
                full_timetable.extend(items)
                
        except Exception as e:
            print(f"   ⚠️ Exception fetching chunk: {e}")

        current_start = current_end + timedelta(days=1)
    
    return full_timetable

def parse_webuntis_time(date_int, time_int):
    """Convert WebUntis date/time ints to datetime object"""
    date_str = str(date_int)
    time_str = str(time_int).zfill(4)
    return datetime.strptime(f"{date_str}{time_str}", "%Y%m%d%H%M")

# --- MERGING LOGIC ---

class ProcessedLesson:
    """Helper class to manage lesson data for merging"""
    def __init__(self, raw_lesson):
        self.id = raw_lesson['id']
        self.date = raw_lesson['date']
        self.start_time = raw_lesson['startTime']
        self.end_time = raw_lesson['endTime']
        
        # Determine Subject Name (Key for merging)
        subjects = raw_lesson.get('su', [])
        self.subject_name = subjects[0].get('longname') or subjects[0].get('name') if subjects else "Lesson"
        
        # Use Sets to avoid duplicates when merging (e.g. Serge + Serge = Serge)
        self.subjects = {su.get('longname') or su.get('name', '') for su in subjects}
        self.teachers = {te.get('longname') or te.get('name', '') for te in raw_lesson.get('te', [])}
        self.rooms = {ro.get('longname') or ro.get('name', '') for ro in raw_lesson.get('ro', [])}
        self.classes = {kl.get('longname') or kl.get('name', '') for kl in raw_lesson.get('kl', [])}
        
        self.info = raw_lesson.get('info', '')
        self.subst_text = raw_lesson.get('substText', '')
        self.code = raw_lesson.get('code', '') # e.g. 'cancelled'

    @property
    def start_dt(self):
        return parse_webuntis_time(self.date, self.start_time)

    @property
    def end_dt(self):
        return parse_webuntis_time(self.date, self.end_time)

    def merge_with(self, other):
        """Merge details from another overlapping lesson into this one"""
        self.subjects.update(other.subjects)
        self.teachers.update(other.teachers)
        self.rooms.update(other.rooms)
        self.classes.update(other.classes)
        # Combine info if unique
        if other.info and other.info not in self.info:
            self.info = f"{self.info} | {other.info}" if self.info else other.info

def process_timetable(raw_timetable):
    """
    1. Filter cancellations
    2. Convert to objects
    3. Merge overlaps (same time, same subject) -> Union of teachers/classes
    4. Merge adjacent (same subject/teachers, continuous time) -> Extend end time
    """
    # 1. Convert to ProcessedLesson objects and filter cancelled
    lessons = []
    for raw in raw_timetable:
        if raw.get('code') == 'cancelled':
            continue
        try:
            lessons.append(ProcessedLesson(raw))
        except ValueError:
            continue

    if not lessons:
        return []

    # Sort primarily by start time
    lessons.sort(key=lambda x: (x.start_dt, x.subject_name))

    # 2. HORIZONTAL MERGE: Combine items at the EXACT SAME time and Subject
    # Key: (start_dt, end_dt, subject_name)
    merged_overlaps = {}
    
    for lesson in lessons:
        key = (lesson.start_dt, lesson.end_dt, lesson.subject_name)
        
        if key in merged_overlaps:
            # Merge into existing entry
            merged_overlaps[key].merge_with(lesson)
        else:
            merged_overlaps[key] = lesson

    # Convert back to list and sort again
    consolidated_list = sorted(merged_overlaps.values(), key=lambda x: x.start_dt)

    # 3. VERTICAL MERGE: Combine adjacent blocks (e.g. 9-10 and 10-11)
    if not consolidated_list:
        return []

    final_lessons = [consolidated_list[0]]

    for current in consolidated_list[1:]:
        previous = final_lessons[-1]

        # Check conditions for merging:
        # 1. Same Date
        # 2. Previous End == Current Start (Continuous)
        # 3. Same Subject Name
        # 4. Same Teachers (Set comparison)
        # 5. Same Rooms (Set comparison)
        # 6. Same Classes (Set comparison)
        
        is_continuous = (previous.end_dt == current.start_dt)
        is_same_content = (
            previous.subject_name == current.subject_name and
            previous.teachers == current.teachers and
            previous.rooms == current.rooms and
            previous.classes == current.classes
        )

        if is_continuous and is_same_content:
            # Extend the previous lesson's end time
            previous.end_time = current.end_time
            # (Date remains the same)
        else:
            final_lessons.append(current)

    return final_lessons

# --- ICS GENERATION ---

def sync_calendar():
    """Main function"""
    config = load_config()
    
    if not config:
        raise Exception("Configuration not found.")

    print("🔐 Logging in...")
    session, session_id = webuntis_login(config)
    
    print("🔍 Finding element...")
    element_id, element_type = get_element_id(session, config, session_id)
    
    # Date range
    today = datetime.now().date()
    start_date = today - timedelta(days=180)
    end_date = today + timedelta(days=180)
    
    print(f"📅 Fetching raw data {start_date} to {end_date}...")
    raw_timetable = get_timetable(session, config, session_id, element_id, element_type, start_date, end_date)
    
    print(f"⚙️ Processing and merging {len(raw_timetable)} raw items...")
    processed_lessons = process_timetable(raw_timetable)
    
    # Setup Calendar
    cal = Calendar()
    cal.add('prodid', '-//WebUntis Sync//webuntis-sync//EN')
    cal.add('version', '2.0')
    cal.add('x-wr-calname', 'WebUntis Timetable')
    cal.add('x-wr-timezone', 'Europe/Brussels')
    timezone = pytz.timezone('Europe/Brussels')
    
    for lesson in processed_lessons:
        event = Event()
        
        # Prepare lists for display (Sort to keep consistent)
        s_subjects = sorted(list(lesson.subjects))
        s_teachers = sorted(list(lesson.teachers))
        s_classes = sorted(list(lesson.classes))
        s_rooms = sorted(list(lesson.rooms))
        
        # Summary
        summary = ', '.join(s_subjects) if s_subjects else 'Lesson'
        if lesson.subst_text:
            summary = f"{summary} ({lesson.subst_text})"
        
        event.add('summary', summary)
        event.add('dtstart', timezone.localize(lesson.start_dt))
        event.add('dtend', timezone.localize(lesson.end_dt))
        
        # Description: Teachers / Classes
        description_parts = []
        if s_teachers:
            description_parts.append(' / '.join(s_teachers))
        if s_classes:
            description_parts.append(' / '.join(s_classes))
        if lesson.info:
            description_parts.append(str(lesson.info))
        if lesson.subst_text:
            description_parts.append(str(lesson.subst_text))
            
        if description_parts:
            event.add('description', '\n'.join(description_parts))
            
        # Location
        if s_rooms:
            event.add('location', ', '.join(s_rooms))
            
        # UID
        uid = f"{lesson.id}-{lesson.date}-{lesson.start_time}@webuntis-sync"
        event.add('uid', uid)
        
        cal.add_component(event)
    
    # Save
    os.makedirs('docs', exist_ok=True)
    with open('docs/calendar.ics', 'wb') as f:
        f.write(cal.to_ical())
    
    print(f"✅ Calendar synced: {len(processed_lessons)} events (merged from {len(raw_timetable)} items).")

if __name__ == '__main__':
    try:
        sync_calendar()
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)
