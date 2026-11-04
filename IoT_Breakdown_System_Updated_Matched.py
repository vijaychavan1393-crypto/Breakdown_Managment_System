import tkinter as tk
from tkinter import ttk, messagebox
from flask import Flask, request, jsonify
import threading, sqlite3, json, time, os, re
from datetime import datetime, date
from pathlib import Path
from collections import deque

# ---------------- CONFIGURATION ----------------
HOST, PORT = "0.0.0.0", 5000
DB_FILE = "breakdown_history.db"
OFFLINE_SECONDS = 15
SLIDE_SECONDS = 6
ROOT_DIR = Path(__file__).resolve().parent
MACHINE_DIR = ROOT_DIR / "machine_list"
SLIP_DIR = ROOT_DIR / "breakdown_slips"
MACHINE_FILE = MACHINE_DIR / "machines.json"

ALARMS = {
    "emg_stop": "EMG STOP BUTTON PUSHED",
    "air_pressure_low": "AIR PRESSURE LOW",
    "lub_pressure_low": "LUBRICATION PRESSURE LOW",
    "lub_motor_trip": "LUBRICATION MOTOR TRIP",
    "main_motor_trip": "MAIN MOTOR TRIP",
    "hol_pressure_drop": "HOL PRESSURE DROP"
}
BG, PANEL, BLUE = "#152B40", "#173D5F", "#168BFF"
WHITE, MUTED, GREEN = "#F3F8FF", "#A8BED1", "#20D875"
RED, AMBER, CYAN = "#FF454F", "#FFB020", "#39C7FF"

MACHINE_DIR.mkdir(exist_ok=True)
SLIP_DIR.mkdir(exist_ok=True)
app = Flask(__name__)
db_lock = threading.RLock()
ui_events = deque(maxlen=1000)
machine_lock = threading.RLock()

DEFAULT_MACHINES = [
    {"line": "LINE 1", "number": "M-01", "name": "500 TON PRESS",
     "machine_id": "LINE1_500TON_PRESS", "enabled": True},
    {"line": "LINE 2", "number": "M-02", "name": "250 TON PRESS",
     "machine_id": "LINE2_250TON_PRESS", "enabled": True},
]

def timestamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def safe_id(value):
    return re.sub(r"[^A-Za-z0-9_-]", "_", value)[:80] or "machine"

def load_machines():
    with machine_lock:
        if not MACHINE_FILE.exists():
            save_machines(DEFAULT_MACHINES)
        try:
            data = json.loads(MACHINE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            pass
        return []

def save_machines(items):
    with machine_lock:
        tmp = MACHINE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(MACHINE_FILE)

def find_machine(machine_id):
    return next((m for m in load_machines()
                 if m.get("enabled", True) and m.get("machine_id") == machine_id), None)

def machine_matches(data, configured):
    # Exact identity match required: ID + line + number + name.
    return all(str(data.get(k, "")).strip().casefold() ==
               str(configured.get(k, "")).strip().casefold()
               for k in ("machine_id", "line", "number", "name"))

def connect_db():
    con = sqlite3.connect(DB_FILE, timeout=10)
    con.row_factory = sqlite3.Row
    return con

def parse_time(value):
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")

def format_duration(start_value, end_value):
    try:
        seconds=max(0,int((parse_time(end_value)-parse_time(start_value)).total_seconds()))
        days,rem=divmod(seconds,86400)
        hours,rem=divmod(rem,3600)
        minutes,secs=divmod(rem,60)
        return f"{days}d {hours:02d}:{minutes:02d}:{secs:02d}" if days else f"{hours:02d}:{minutes:02d}:{secs:02d}"
    except (ValueError, TypeError):
        return ""

def init_db():
    with connect_db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS machine_status (
            machine_id TEXT PRIMARY KEY, line TEXT, machine_number TEXT, machine_name TEXT,
            alarms_json TEXT NOT NULL, last_seen TEXT, online INTEGER DEFAULT 0)""")
        con.execute("""CREATE TABLE IF NOT EXISTS alarm_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, machine_id TEXT NOT NULL,
            alarm_key TEXT NOT NULL, alarm_name TEXT NOT NULL, active_time TEXT NOT NULL,
            solved_time TEXT, duration_seconds INTEGER, status TEXT NOT NULL)""")
        # Migrate a database created by the earlier version.
        cols = {r["name"] for r in con.execute("PRAGMA table_info(machine_status)")}
        for col, typ in [("line", "TEXT"), ("machine_number", "TEXT"), ("machine_name", "TEXT")]:
            if col not in cols:
                con.execute(f"ALTER TABLE machine_status ADD COLUMN {col} {typ}")
        for m in load_machines():
            con.execute("""INSERT OR IGNORE INTO machine_status
                (machine_id,line,machine_number,machine_name,alarms_json,last_seen,online)
                VALUES (?,?,?,?,?,'Never',0)""",
                (m["machine_id"], m.get("line",""), m.get("number",""), m.get("name",""),
                 json.dumps({k: False for k in ALARMS})))

def ensure_machine_row(m):
    with connect_db() as con:
        con.execute("""INSERT OR IGNORE INTO machine_status
            (machine_id,line,machine_number,machine_name,alarms_json,last_seen,online)
            VALUES (?,?,?,?,?,'Never',0)""",
            (m["machine_id"], m.get("line",""), m.get("number",""), m.get("name",""),
             json.dumps({k: False for k in ALARMS})))

# ---------------- FLASK API ----------------
@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": timestamp()})

@app.route("/machine", methods=["POST"])
def receive_machine():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "Request body must be a JSON object"}), 400
    mid = str(data.get("machine_id", "")).strip()
    configured = find_machine(mid)
    if not configured:
        return jsonify({"ok": False, "error": "Machine ID is not in enabled machine list"}), 400
    if not machine_matches(data, configured):
        return jsonify({"ok": False, "error": "Machine identity mismatch; data rejected"}), 409
    alarms = data.get("alarms", {})
    if not isinstance(alarms, dict):
        return jsonify({"ok": False, "error": "alarms must be an object"}), 400

    # ESP32 sends JSON true/false. Accept only actual JSON booleans to avoid
    # treating strings such as "false" as an active alarm.
    new_state = {key: (alarms.get(key) is True) for key in ALARMS}
    event_time = timestamp()
    ensure_machine_row(configured)
    with db_lock, connect_db() as con:
        row = con.execute("SELECT alarms_json FROM machine_status WHERE machine_id=?", (mid,)).fetchone()
        old_state = json.loads(row["alarms_json"]) if row else {k: False for k in ALARMS}
        con.execute("""UPDATE machine_status SET line=?, machine_number=?, machine_name=?,
            alarms_json=?, last_seen=?, online=1 WHERE machine_id=?""",
            (configured.get("line",""), configured.get("number",""), configured.get("name",""),
             json.dumps(new_state), event_time, mid))
        for key, active in new_state.items():
            previous = bool(old_state.get(key, False))
            if active and not previous:
                con.execute("""INSERT INTO alarm_history
                    (machine_id,alarm_key,alarm_name,active_time,solved_time,duration_seconds,status)
                    VALUES (?,?,?,?,NULL,NULL,'ACTIVE')""", (mid,key,ALARMS[key],event_time))
                ui_events.append({"type":"ACTIVE","machine_id":mid,"alarm_key":key,"time":event_time})
            elif previous and not active:
                active_row = con.execute("""SELECT id,active_time FROM alarm_history
                    WHERE machine_id=? AND alarm_key=? AND status='ACTIVE'
                    ORDER BY id DESC LIMIT 1""", (mid,key)).fetchone()
                if active_row:
                    duration = max(0, int((parse_time(event_time)-parse_time(active_row["active_time"])).total_seconds()))
                    con.execute("""UPDATE alarm_history SET solved_time=?,duration_seconds=?,status='SOLVED'
                        WHERE id=?""", (event_time,duration,active_row["id"]))
                    ui_events.append({"type":"SOLVED","machine_id":mid,"alarm_key":key,"time":event_time})
    return jsonify({"ok":True,"machine_id":mid,"received_at":event_time,"alarms":new_state})

@app.route("/status")
def get_status():
    with db_lock, connect_db() as con:
        rows = con.execute("SELECT * FROM machine_status").fetchall()
    now_dt = datetime.now()
    configured = {m["machine_id"]: m for m in load_machines()}
    result = []
    for row in rows:
        d = dict(row)
        m = configured.get(d["machine_id"])
        # Only expose configured and exactly matching machines.
        if not m or not machine_matches({
            "machine_id": d["machine_id"], "line": d.get("line",""),
            "number": d.get("machine_number",""), "name": d.get("machine_name","")
        }, m):
            continue
        d["machine"] = {"line":m["line"],"number":m["number"],"name":m["name"]}
        d["alarms"] = json.loads(d.pop("alarms_json"))
        try: d["online"] = (now_dt - parse_time(d["last_seen"])).total_seconds() <= OFFLINE_SECONDS
        except (ValueError, TypeError): d["online"] = False
        result.append(d)
    # Include registered machines even if no heartbeat yet.
    existing = {x["machine_id"] for x in result}
    for m in load_machines():
        if m.get("enabled", True) and m["machine_id"] not in existing:
            result.append({"machine_id":m["machine_id"],"line":m["line"],
                "machine_number":m["number"],"machine_name":m["name"],
                "machine":{"line":m["line"],"number":m["number"],"name":m["name"]},
                "alarms":{k:False for k in ALARMS},"last_seen":"Never","online":False})
    return jsonify(result)

@app.route("/history")
def get_history():
    limit = min(max(request.args.get("limit", default=100, type=int), 1), 500)
    with db_lock, connect_db() as con:
        rows = con.execute("SELECT * FROM alarm_history ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    machines = {m["machine_id"]:m for m in load_machines()}
    result=[]
    for row in rows:
        d=dict(row); m=machines.get(d["machine_id"])
        if not m: continue
        d["machine"]={"line":m["line"],"number":m["number"],"name":m["name"]}
        result.append(d)
    return jsonify(result)

@app.route("/events")
def get_events():
    result=list(ui_events); ui_events.clear()
    return jsonify(result)

# ---------------- BREAKDOWN SLIP FILES ----------------
def list_slips():
    slips=[]
    for p in SLIP_DIR.glob("*.json"):
        if p.name.startswith("_"):
            continue
        try: slips.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception: continue
    return sorted(slips, key=lambda x: x.get("created_at",""), reverse=True)

def dashboard_eligible(slip):
    """Only these statuses are allowed on the Page 1 sent-slip banner."""
    status = re.sub(r"\s+", " ", str(slip.get("status", "")).strip().upper().replace("_", " "))
    return status in {"OPEN", "IN PROGRESS", "ON HOLD"} and bool(slip.get("sent_to_dashboard"))

@app.route("/slips", methods=["GET"])
def api_slips():
    return jsonify(list_slips())

@app.route("/slips", methods=["POST"])
def save_slip_api():
    data=request.get_json(silent=True) or {}
    slip_id=data.get("slip_id") or datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    data["slip_id"]=safe_id(slip_id)
    data["created_at"]=data.get("created_at") or timestamp()
    path=SLIP_DIR/(data["slip_id"]+".json")
    path.write_text(json.dumps(data,indent=2,ensure_ascii=False),encoding="utf-8")
    return jsonify({"ok":True,"slip_id":data["slip_id"]})

# ---------------- DASHBOARD / UI ----------------
class Dashboard:
    def __init__(self, root):
        self.root=root
        root.title("IoT Based Monitoring System")
        root.geometry("1600x920"); root.minsize(1100,700); root.configure(bg=BG)
        self.status=[]; self.history=[]; self.active_rows=[]
        self.slide_index=0; self.last_active_ids=set()
        self.slips=[]; self.slip_index=0; self.current_alarm_prefill=None; self.current_slip_id=None
        self.latest_sent_slip = next((x for x in list_slips() if dashboard_eligible(x)), None)
        self.setup_style()
        self.build_notebook()
        self.refresh(); self.rotate_slides(); self.update_clock()
        self.refresh_machine_table()

    def setup_style(self):
        style=ttk.Style(); style.theme_use("clam")
        style.configure("Treeview",background="#0B1E31",foreground=WHITE,
            fieldbackground="#0B1E31",rowheight=26,font=("Segoe UI",9))
        style.configure("Treeview.Heading",background="#16466A",foreground="white",
            font=("Segoe UI",9,"bold"))
        style.map("Treeview",background=[("selected","#1677B8")])
        style.configure("TNotebook",background=BG,borderwidth=0)
        style.configure("TNotebook.Tab",padding=(18,8),font=("Segoe UI",10,"bold"))

    def build_notebook(self):
        self.nb=ttk.Notebook(self.root); self.nb.pack(fill="both",expand=True,padx=8,pady=8)
        self.page1=tk.Frame(self.nb,bg=BG); self.page2=tk.Frame(self.nb,bg=BG)
        self.page3=tk.Frame(self.nb,bg=BG); self.page4=tk.Frame(self.nb,bg=BG)
        self.nb.add(self.page1,text="PAGE 1 — LIVE DASHBOARD")
        self.nb.add(self.page2,text="PAGE 2 — BREAKDOWN SLIP")
        self.nb.add(self.page3,text="PAGE 3 — MACHINE LIST")
        self.nb.add(self.page4,text="PAGE 4 — MACHINE HISTORY")
        self.build_dashboard_page(); self.build_slip_page(); self.build_machine_page(); self.build_machine_history_page()

    def build_dashboard_page(self):
        header=tk.Frame(self.page1,bg="#0B2A45"); header.pack(fill="x",pady=(0,5))
        tk.Label(header,text="IoT BASED MONITORING SYSTEM",bg="#0B2A45",fg=WHITE,
            font=("Segoe UI",22,"bold")).pack(side="left",padx=20,pady=14)
        self.comm_label=tk.Label(header,text="● SERVER STARTING",bg="#0B2A45",fg=AMBER,
            font=("Segoe UI",12,"bold")); self.comm_label.pack(side="right",padx=20)
        info=tk.Frame(self.page1,bg=BG); info.pack(fill="x",pady=5)
        self.clock_label=tk.Label(info,text="",bg=PANEL,fg=CYAN,font=("Segoe UI",12,"bold"),padx=12,pady=10)
        self.clock_label.pack(side="left")
        kpis=tk.Frame(info,bg=BG); kpis.pack(side="right"); self.kpi_vars={}
        for key,title,color in [("total","TODAY TOTAL",CYAN),("active","ACTIVE ALARMS",RED),("solved","SOLVED ALARMS",GREEN)]:
            card=tk.Frame(kpis,bg=PANEL,padx=14,pady=5); card.pack(side="left",padx=4)
            tk.Label(card,text=title,bg=PANEL,fg=MUTED,font=("Segoe UI",9,"bold")).pack()
            var=tk.StringVar(value="0")
            tk.Label(card,textvariable=var,bg=PANEL,fg=color,font=("Segoe UI",20,"bold")).pack()
            self.kpi_vars[key]=var
        body=tk.Frame(self.page1,bg=BG); body.pack(fill="both",expand=True,pady=5)
        body.columnconfigure(0,weight=5); body.columnconfigure(1,weight=6); body.rowconfigure(0,weight=1)
        left=tk.Frame(body,bg=PANEL,highlightbackground=BLUE,highlightthickness=1)
        left.grid(row=0,column=0,sticky="nsew",padx=(0,5))
        tk.Label(left,text="BREAKDOWN SLIP  |  ACTIVE ALARMS",bg=PANEL,fg=CYAN,font=("Segoe UI",13,"bold")).pack(anchor="w",padx=14,pady=10)
        self.slide_area=tk.Frame(left,bg="#091B2B"); self.slide_area.pack(fill="both",expand=True,padx=12,pady=10)
        self.slide_counter=tk.Label(left,text="SLIDE 0 / 0",bg=PANEL,fg=MUTED); self.slide_counter.pack(pady=(0,10))
        right=tk.Frame(body,bg=BG); right.grid(row=0,column=1,sticky="nsew",padx=(5,0))
        right.rowconfigure(0,weight=5); right.rowconfigure(1,weight=5); right.columnconfigure(0,weight=1)
        ap=tk.Frame(right,bg=PANEL); ap.grid(row=0,column=0,sticky="nsew",pady=(0,5))
        tk.Label(ap,text="ACTIVE ALARM LIVE LIST",bg=PANEL,fg=RED,font=("Segoe UI",12,"bold")).pack(anchor="w",padx=10,pady=8)
        self.active_tree=self.make_tree(ap,("line","number","machine","alarm","since"),
            ("LINE","MACHINE NO.","MACHINE NAME","ACTIVE ALARM","ACTIVE SINCE"),(75,90,145,210,130))
        sp=tk.Frame(right,bg=PANEL); sp.grid(row=1,column=0,sticky="nsew",pady=(5,0))
        tk.Label(sp,text="SOLVED ALARM HISTORY — TODAY",bg=PANEL,fg=GREEN,font=("Segoe UI",12,"bold")).pack(anchor="w",padx=10,pady=8)
        self.solved_tree=self.make_tree(sp,("line","number","machine","alarm","active","solved","duration"),
            ("LINE","MACHINE NO.","MACHINE","ALARM","ACTIVE TIME","SOLVED TIME","DURATION"),(55,75,100,140,105,105,85))
        tk.Label(self.page1,text="Monitoring only — do not use Wi-Fi/software for safety control.",
            bg="#0B2A45",fg=MUTED,font=("Segoe UI",9),anchor="w",padx=12,pady=7).pack(fill="x",pady=(4,0))

    def make_tree(self,parent,columns,headings,widths):
        frame=tk.Frame(parent,bg=PANEL); frame.pack(fill="both",expand=True,padx=8,pady=(0,8))
        tree=ttk.Treeview(frame,columns=columns,show="headings",height=5)
        for c,h,w in zip(columns,headings,widths):
            tree.heading(c,text=h); tree.column(c,width=w,minwidth=55,anchor="center")
        y=ttk.Scrollbar(frame,orient="vertical",command=tree.yview); tree.configure(yscrollcommand=y.set)
        tree.pack(side="left",fill="both",expand=True); y.pack(side="right",fill="y")
        return tree

    def build_slip_page(self):
        top=tk.Frame(self.page2,bg="#0B2A45"); top.pack(fill="x")
        tk.Label(top,text="BREAKDOWN SLIP REGISTER",bg="#0B2A45",fg=WHITE,font=("Segoe UI",18,"bold")).pack(side="left",padx=15,pady=12)
        main=tk.Frame(self.page2,bg=BG); main.pack(fill="both",expand=True,padx=12,pady=10)
        main.columnconfigure(0,weight=1); main.columnconfigure(1,weight=1)
        form=tk.LabelFrame(main,text="Breakdown Slip Details",bg=PANEL,fg=CYAN,font=("Segoe UI",11,"bold"),padx=12,pady=10)
        form.grid(row=0,column=0,sticky="nsew",padx=(0,8))
        self.slip_vars={k:tk.StringVar() for k in ["line","number","name","description","action","status","start","end","source_alarm"]}
        self.slip_machine_id=tk.StringVar()
        fields=[("Line number","line"),("Machine number","number"),("Machine name","name"),
                ("Breakdown description","description"),("Action taken","action"),("Status","status"),
                ("Breakdown start time","start"),("Breakdown end time","end")]
        self.machine_choice=ttk.Combobox(form,state="readonly",width=38)
        self.machine_choice.grid(row=0,column=1,sticky="ew",pady=4)
        tk.Label(form,text="Available machine",bg=PANEL,fg=WHITE).grid(row=0,column=0,sticky="w",pady=4)
        self.machine_choice.bind("<<ComboboxSelected>>",self.on_slip_machine_select)
        for i,(label,key) in enumerate(fields,1):
            tk.Label(form,text=label,bg=PANEL,fg=WHITE).grid(row=i,column=0,sticky="w",pady=4)
            if key=="description":
                w=tk.Entry(form,textvariable=self.slip_vars[key],width=40)
            elif key=="action":
                w=tk.Entry(form,textvariable=self.slip_vars[key],width=40)
            elif key=="status":
                w=ttk.Combobox(form,textvariable=self.slip_vars[key],values=("OPEN","IN PROGRESS","CLOSED","ON HOLD"),width=37)
            else:
                w=tk.Entry(form,textvariable=self.slip_vars[key],width=40)
            w.grid(row=i,column=1,sticky="ew",pady=4)
        form.columnconfigure(1,weight=1)
        tk.Label(form,text="Date/time format: YYYY-MM-DD HH:MM:SS",bg=PANEL,fg=MUTED).grid(row=9,column=0,columnspan=2,sticky="w",pady=4)
        tk.Label(form,textvariable=self.slip_vars["source_alarm"],bg=PANEL,fg=AMBER,wraplength=500).grid(row=10,column=0,columnspan=2,sticky="w",pady=5)
        buttons=tk.Frame(form,bg=PANEL); buttons.grid(row=11,column=0,columnspan=2,sticky="ew",pady=8)
        for label,cmd in [("Send",self.send_slip),("Save",self.save_slip),("View Saved Slips",self.view_slips),
                          ("Previous",self.previous_slip),("Next",self.next_slip),("Delete Selected Slip",self.delete_selected_slip),("Print Saved Slips A4",self.print_saved_slips),("Use Current Alarm",self.use_current_alarm)]:
            tk.Button(buttons,text=label,command=cmd,bg="#16466A",fg=WHITE,activebackground=BLUE,relief="flat",padx=8,pady=6).pack(side="left",padx=3)
        listing=tk.LabelFrame(main,text="Saved Breakdown Slips",bg=PANEL,fg=GREEN,font=("Segoe UI",11,"bold"),padx=8,pady=8)
        listing.grid(row=0,column=1,sticky="nsew")
        self.slip_tree=self.make_tree(listing,("id","line","number","name","description","status","active_time","solved_time","duration"),
            ("SLIP ID","LINE","MACHINE NO.","MACHINE","DESCRIPTION","STATUS","ALARM ACTIVE TIME","SOLVED TIME","DURATION"),(100,65,85,110,170,90,145,145,100))
        self.slip_tree.bind("<<TreeviewSelect>>",self.select_slip_from_tree)
        self.slip_count_label=tk.Label(listing,text="Total saved slips: 0 | Completed: 0",
            bg=PANEL,fg=CYAN,font=("Segoe UI",10,"bold"))
        self.slip_count_label.pack(anchor="w",pady=(2,4))
        tk.Label(listing,text="Slips are stored as individual JSON files in the breakdown_slips folder. Page 1 solved alarms are also added here as CLOSED slips.",
            bg=PANEL,fg=MUTED,wraplength=500).pack(anchor="w",pady=5)
        self.refresh_machine_choices(); self.refresh_slip_list()

    def build_machine_page(self):
        top=tk.Frame(self.page3,bg="#0B2A45"); top.pack(fill="x")
        tk.Label(top,text="MACHINE LIST MANAGEMENT",bg="#0B2A45",fg=WHITE,font=("Segoe UI",18,"bold")).pack(side="left",padx=15,pady=12)
        area=tk.Frame(self.page3,bg=BG); area.pack(fill="both",expand=True,padx=12,pady=10)
        edit=tk.LabelFrame(area,text="Add / Edit Machine",bg=PANEL,fg=CYAN,font=("Segoe UI",11,"bold"),padx=12,pady=10)
        edit.pack(fill="x",pady=(0,10))
        self.machine_vars={k:tk.StringVar() for k in ["line","number","name","machine_id","make","details"]}
        machine_fields=[("Line number (e.g. LINE 1)","line"),("Machine number (e.g. M-01)","number"),
                        ("Machine name (e.g. 500 TON PRESS)","name"),("Machine ID (unique ESP32 ID)","machine_id"),
                        ("Machine make / manufacturer","make"),("Machine details / specification","details")]
        for i,(label,key) in enumerate(machine_fields):
            r=i//2; c=(i%2)*2
            tk.Label(edit,text=label,bg=PANEL,fg=WHITE).grid(row=r,column=c,sticky="w",padx=5,pady=5)
            if key=="details":
                w=tk.Entry(edit,textvariable=self.machine_vars[key],width=34)
            else:
                w=tk.Entry(edit,textvariable=self.machine_vars[key],width=34)
            w.grid(row=r,column=c+1,sticky="ew",padx=5,pady=5)
        edit.columnconfigure(1,weight=1); edit.columnconfigure(3,weight=1)
        buttons=tk.Frame(edit,bg=PANEL); buttons.grid(row=3,column=0,columnspan=4,sticky="w",pady=5)
        for label,cmd in [("Add Machine",self.add_machine),("Update Selected",self.update_machine),
                          ("Remove Selected",self.remove_machine),("Search Machine",self.search_machine),
                          ("Show Total Machine List",self.show_all_machines),("Clear Fields",self.clear_machine_fields),
                          ("Print A4 Machine List",self.print_machine_list)]:
            tk.Button(buttons,text=label,command=cmd,bg="#16466A",fg=WHITE,relief="flat",padx=10,pady=6).pack(side="left",padx=4)
        listing=tk.LabelFrame(area,text="Registered Machines",bg=PANEL,fg=GREEN,font=("Segoe UI",11,"bold"),padx=8,pady=8)
        listing.pack(fill="both",expand=True)
        self.machine_tree=self.make_tree(listing,("line","number","name","make","details","machine_id","enabled"),
            ("LINE","MACHINE NO.","MACHINE NAME","MAKE","MACHINE DETAILS","ESP32 MACHINE ID","ENABLED"),(95,95,155,130,230,210,65))
        self.machine_tree.bind("<<TreeviewSelect>>",self.select_machine)
        tk.Label(listing,text=f"Saved in: {MACHINE_FILE}",bg=PANEL,fg=MUTED,anchor="w").pack(fill="x",pady=5)

    # ---------------- PAGE 4 — MACHINE HISTORY ----------------
    def build_machine_history_page(self):
        top = tk.Frame(self.page4, bg="#0B2A45")
        top.pack(fill="x")
        tk.Label(top, text="MACHINE HISTORY", bg="#0B2A45", fg=WHITE,
                 font=("Segoe UI", 18, "bold")).pack(side="left", padx=15, pady=12)

        controls = tk.Frame(self.page4, bg=BG)
        controls.pack(fill="x", padx=14, pady=(12, 8))
        tk.Label(controls, text="Machine History", bg=BG, fg=CYAN,
                 font=("Segoe UI", 12, "bold")).pack(side="left", padx=(0, 8))

        self.history_machine_choice = ttk.Combobox(
            controls, state="readonly", width=58, font=("Segoe UI", 10)
        )
        self.history_machine_choice.pack(side="left", padx=5)
        tk.Button(controls, text="Search", command=self.search_machine_history,
                  bg="#1677B8", fg=WHITE, relief="flat", padx=18, pady=7).pack(side="left", padx=8)
        tk.Button(controls, text="Print A4", command=self.print_machine_history,
                  bg="#16804A", fg=WHITE, relief="flat", padx=18, pady=7).pack(side="right", padx=5)

        self.machine_history_summary = tk.Label(
            self.page4, text="Select a machine and press Search.",
            bg=BG, fg=MUTED, font=("Segoe UI", 10, "bold"), anchor="w"
        )
        self.machine_history_summary.pack(fill="x", padx=16, pady=(0, 8))

        listing = tk.LabelFrame(
            self.page4, text="Selected Machine — Complete Breakdown / Alarm History",
            bg=PANEL, fg=GREEN, font=("Segoe UI", 11, "bold"), padx=8, pady=8
        )
        listing.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        cols = ("line", "name", "number", "make", "details", "breakdown",
                "action", "start", "end", "duration", "status")
        heads = ("LINE", "MACHINE NAME", "MACHINE NO.", "MACHINE MAKE", "MACHINE DETAILS",
                 "BREAKDOWN / ALARM", "ACTION TAKEN", "START TIME", "END TIME",
                 "DURATION", "STATUS")
        widths = (85, 140, 90, 125, 180, 210, 210, 145, 145, 115, 90)
        self.machine_history_tree = self.make_tree(listing, cols, heads, widths)
        self.machine_history_rows = []
        self.refresh_machine_history_choices()

    def refresh_machine_history_choices(self):
        if not hasattr(self, "history_machine_choice"):
            return
        machines = [m for m in load_machines() if m.get("enabled", True)]
        self.history_machine_map = {}
        values = []
        for m in machines:
            label = f'{m.get("line","")} | {m.get("name","")} | {m.get("number","")} | {m.get("machine_id","")}'
            values.append(label)
            self.history_machine_map[label] = m
        self.history_machine_choice["values"] = values
        if values and not self.history_machine_choice.get():
            self.history_machine_choice.current(0)

    def search_machine_history(self):
        selected = self.history_machine_choice.get().strip()
        machine = getattr(self, "history_machine_map", {}).get(selected)
        if not machine:
            messagebox.showwarning("Select machine", "Please select an available machine first.")
            return

        mid = machine.get("machine_id", "")
        line, number, name = machine.get("line", ""), machine.get("number", ""), machine.get("name", "")
        make, details = machine.get("make", ""), machine.get("details", "")
        rows = []

        # Native ESP32 alarm history.
        try:
            with db_lock, connect_db() as con:
                alarm_rows = con.execute(
                    "SELECT * FROM alarm_history WHERE machine_id=? ORDER BY active_time DESC, id DESC",
                    (mid,)
                ).fetchall()
        except Exception as exc:
            messagebox.showerror("History read error", f"Could not read machine alarm history:\\n{exc}")
            return

        matched_alarm_ids = set()
        for item in alarm_rows:
            h = dict(item)
            matched_alarm_ids.add(str(h.get("id", "")))
            active = h.get("active_time", "") or ""
            solved = h.get("solved_time", "") or ""
            duration = format_duration(active, solved) if solved else "ACTIVE"
            rows.append({
                "line": line, "name": name, "number": number, "make": make, "details": details,
                "breakdown": h.get("alarm_name", ""), "action": "Machine alarm cleared by ESP32",
                "start": active, "end": solved, "duration": duration,
                "status": h.get("status", "ACTIVE"), "_sort": active, "_source": "ESP32"
            })

        # Manually sent breakdown slips. Exclude auto-imported Page-1 copies when the
        # corresponding ESP32 alarm_history row is already included, avoiding duplicates.
        for slip in list_slips():
            same_machine = (
                str(slip.get("machine_id", "")) == mid or
                (str(slip.get("line", "")) == line and
                 str(slip.get("number", "")) == number and
                 str(slip.get("name", "")) == name)
            )
            if not same_machine:
                continue
            source_alarm_id = str(slip.get("source_alarm_id", ""))
            if slip.get("imported_from_page1") and source_alarm_id in matched_alarm_ids:
                continue

            active = slip.get("alarm_active_time") or slip.get("start") or slip.get("created_at", "")
            solved = slip.get("solved_time") or slip.get("end", "")
            duration = slip.get("duration") or (format_duration(active, solved) if solved else "ACTIVE")
            rows.append({
                "line": line, "name": name, "number": number, "make": make, "details": details,
                "breakdown": slip.get("description", ""),
                "action": slip.get("action", ""),
                "start": active, "end": solved, "duration": duration,
                "status": slip.get("status", ""), "_sort": active, "_source": "BREAKDOWN SLIP"
            })

        rows.sort(key=lambda r: r.get("_sort", ""), reverse=True)
        self.machine_history_rows = rows
        display = [tuple(r.get(k, "") for k in
                   ("line", "name", "number", "make", "details", "breakdown",
                    "action", "start", "end", "duration", "status")) for r in rows]
        self.fill_tree(self.machine_history_tree, display)
        self.machine_history_summary.config(
            text=f'Line: {line}    |    Machine: {name}    |    Machine No.: {number}    |    Make: {make}    |    Total history records: {len(rows)}'
        )

    def print_machine_history(self):
        if not getattr(self, "machine_history_rows", None):
            messagebox.showinfo("Machine History", "Search a machine first. No history records are available to print.")
            return
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill, Border, Side, Alignment
            from openpyxl.utils import get_column_letter
        except ImportError:
            messagebox.showerror("Excel library missing", "Install openpyxl using:\\npython -m pip install openpyxl")
            return

        rows = self.machine_history_rows
        first = rows[0]
        machine_title = f'{first["line"]} - {first["name"]} ({first["number"]})'
        safe_name = safe_id(first["number"] or first["name"] or "machine")
        out_path = MACHINE_DIR / f"Machine_History_{safe_name}.xlsx"

        headers = ["Sr. No.", "Line", "Machine Name", "Machine No.", "Machine Make",
                   "Machine Details", "Breakdown / Alarm", "Action Taken", "Start Time",
                   "End Time", "Duration", "Status"]
        wb = Workbook()
        ws = wb.active
        ws.title = "Machine History"
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
        c = ws.cell(1, 1, "MACHINE BREAKDOWN & ALARM HISTORY")
        c.font = Font(name="Arial", size=16, bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="16466A")
        c.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 30
        ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(headers))
        c = ws.cell(2, 1, f"{machine_title}    |    Generated: {timestamp()}    |    Records: {len(rows)}")
        c.font = Font(name="Arial", size=9, italic=True, color="334155")
        c.alignment = Alignment(horizontal="center")
        header_row = 4
        for col, heading in enumerate(headers, 1):
            c = ws.cell(header_row, col, heading)
            c.font = Font(name="Arial", size=9, bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="2878A8")
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

        thin = Side(style="thin", color="9AAFC0")
        keys = ("line", "name", "number", "make", "details", "breakdown",
                "action", "start", "end", "duration", "status")
        for idx, record in enumerate(rows, 1):
            values = [idx] + [record.get(k, "") for k in keys]
            excel_row = header_row + idx
            for col, value in enumerate(values, 1):
                c = ws.cell(excel_row, col, value)
                c.font = Font(name="Arial", size=8, color="172B3A")
                c.alignment = Alignment(vertical="top", wrap_text=True)
                c.border = Border(left=thin, right=thin, top=thin, bottom=thin)
                if idx % 2 == 0:
                    c.fill = PatternFill("solid", fgColor="EAF2F8")
            ws.row_dimensions[excel_row].height = 38

        widths = [7, 12, 20, 13, 20, 30, 30, 30, 21, 21, 17, 14]
        for col, width in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(col)].width = width
        ws.row_dimensions[header_row].height = 34
        ws.freeze_panes = "A5"
        ws.auto_filter.ref = f"A4:L{len(rows) + 4}"
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.page_setup.orientation = "landscape"
        ws.page_setup.paperSize = ws.PAPERSIZE_A4
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.page_margins.left = 0.2
        ws.page_margins.right = 0.2
        ws.page_margins.top = 0.4
        ws.page_margins.bottom = 0.4
        ws.print_title_rows = "1:4"
        ws.print_area = f"A1:L{len(rows) + 4}"
        ws.oddFooter.center.text = "Page &P of &N"
        ws.oddFooter.right.text = "IoT Breakdown Monitoring System"
        try:
            wb.save(out_path)
        except Exception as exc:
            messagebox.showerror("Excel export failed", f"Could not save Excel file:\\n{exc}")
            return

        try:
            if os.name == "nt":
                os.startfile(str(out_path))
            else:
                import webbrowser
                webbrowser.open(out_path.resolve().as_uri())
        except Exception:
            pass
        messagebox.showinfo("A4 Excel ready", f"Machine history exported to:\\n{out_path}\\n\\nOpen in Excel and press Ctrl+P. Page setup is A4 landscape.")

    def request_json(self,path):
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}",timeout=2) as r:
            return json.loads(r.read().decode("utf-8"))

    def refresh(self):
        try:
            self.status=self.request_json("/status"); self.history=self.request_json("/history?limit=500")
            self.comm_label.config(text="● SERVER CONNECTED",fg=GREEN); self.update_dashboard()
            self.process_events()
        except Exception:
            self.comm_label.config(text="● SERVER DISCONNECTED",fg=RED)
        self.root.after(1000,self.refresh)

    def process_events(self):
        try: events=self.request_json("/events")
        except Exception: return
        for e in events:
            if e.get("type")=="ACTIVE":
                m=find_machine(e["machine_id"])
                if m:
                    self.current_alarm_prefill={"machine_id":m["machine_id"],"line":m["line"],"number":m["number"],
                        "name":m["name"],"description":ALARMS.get(e["alarm_key"],e["alarm_key"]),
                        "start":e["time"],"end":"","source_alarm":f"Machine alarm: {ALARMS.get(e['alarm_key'],e['alarm_key'])}"}
                    self.clear_slip(keep_alarm=True)
                    for k,v in self.current_alarm_prefill.items():
                        if k in self.slip_vars: self.slip_vars[k].set(v)
                    self.slip_machine_id.set(m["machine_id"])
                    self.set_machine_choice(m["machine_id"])
                    self.nb.select(self.page2)

    def update_dashboard(self):
        # Live machine alarms from ESP32 telemetry.
        active_rows=[]
        for mr in self.status:
            mid=mr["machine_id"]; m=mr["machine"]; alarms=mr["alarms"]
            for key,active in alarms.items():
                if not active: continue
                event=next((h for h in self.history if h["machine_id"]==mid and h["alarm_key"]==key and h["status"]=="ACTIVE"),None)
                active_rows.append({"id":f"{mid}:{key}","machine_id":mid,"line":m["line"],"number":m["number"],
                    "machine":m["name"],"alarm":ALARMS[key],"since":event["active_time"] if event else mr["last_seen"],
                    "online":mr["online"]})

        # Sent breakdown slips also behave like active alarms on Page 1.
        active_statuses={"OPEN", "IN PROGRESS", "ON HOLD"}
        for slip in list_slips():
            status=re.sub(r"\s+", " ", str(slip.get("status", "")).strip().upper().replace("_", " "))
            if not slip.get("sent_to_dashboard") or status not in active_statuses:
                continue
            sid=str(slip.get("slip_id", ""))
            # Avoid showing a duplicate if the exact same machine alarm is already represented by telemetry.
            active_rows.append({"id":f"SLIP:{sid}", "slip_id":sid,
                "machine_id":slip.get("machine_id", ""), "line":slip.get("line", ""),
                "number":slip.get("number", ""), "machine":slip.get("name", ""),
                "alarm":slip.get("description", "Breakdown slip"),
                "since":slip.get("start") or slip.get("created_at") or timestamp(), "online":True,
                "is_slip":True})

        ids={a["id"] for a in active_rows}
        if ids!=self.last_active_ids: self.slide_index=0; self.last_active_ids=ids
        self.active_rows=active_rows
        self.fill_tree(self.active_tree,[(a["line"],a["number"],a["machine"],a["alarm"],a["since"].split(" ")[-1]) for a in active_rows])
        today=date.today().strftime("%Y-%m-%d")
        solved=[h for h in self.history if h["status"]=="SOLVED" and h["solved_time"] and h["solved_time"].startswith(today)]
        rows=[]
        for h in solved[:100]:
            m=h["machine"]; d=int(h.get("duration_seconds") or 0)
            rows.append((m["line"],m["number"],m["name"],h["alarm_name"],h["active_time"].split(" ")[-1],
                         h["solved_time"].split(" ")[-1],f"{d//3600:02d}:{(d%3600)//60:02d}:{d%60:02d}"))
        # Include manually registered slips that were solved with Action Taken + Save.
        for slip in list_slips():
            status=str(slip.get("status", "")).strip().upper()
            solved_at=slip.get("end", "")
            if status not in {"CLOSED", "COMPLETED", "SOLVED"} or not solved_at or not solved_at.startswith(today):
                continue
            start_at=slip.get("alarm_active_time") or slip.get("start", "")
            try: duration=max(0, int((parse_time(solved_at)-parse_time(start_at)).total_seconds()))
            except Exception: duration=0
            rows.append((slip.get("line", ""), slip.get("number", ""), slip.get("name", ""),
                slip.get("description", "Breakdown slip"), start_at.split(" ")[-1] if start_at else "",
                solved_at.split(" ")[-1], f"{duration//3600:02d}:{(duration%3600)//60:02d}:{duration%60:02d}"))
        self.fill_tree(self.solved_tree,rows[:150])
        activations=[h for h in self.history if h["active_time"].startswith(today)]
        manual_solved=[x for x in list_slips() if str(x.get("status", "")).strip().upper() in {"CLOSED", "COMPLETED", "SOLVED"} and str(x.get("end", "")).startswith(today)]
        self.kpi_vars["total"].set(str(len(activations)+len([x for x in list_slips() if x.get("sent_to_dashboard") and str(x.get("start", "")).startswith(today)])))
        self.kpi_vars["active"].set(str(len(active_rows)))
        self.kpi_vars["solved"].set(str(len(solved)+len(manual_solved)))

    def fill_tree(self,tree,rows):
        for item in tree.get_children(): tree.delete(item)
        for row in rows: tree.insert("","end",values=row)

    def render_slide(self):
        for w in self.slide_area.winfo_children(): w.destroy()
        active=self.active_rows
        if not active:
            tk.Label(self.slide_area,text="ALL MACHINES NORMAL",bg="#091B2B",fg=GREEN,font=("Segoe UI",23,"bold")).pack(expand=True)
            tk.Label(self.slide_area,text="No active breakdown alarms",bg="#091B2B",fg=MUTED,font=("Segoe UI",13)).pack(pady=(0,40))
            self.slide_counter.config(text="SLIDE 0 / 0"); return
        self.slide_index%=len(active); a=active[self.slide_index]
        for txt,color,size in [("🚨 BREAKDOWN SLIP",RED,16),(a["line"],CYAN,18),(f'MACHINE NO. {a["number"]}',WHITE,12),
                               (a["machine"],WHITE,22),(a["alarm"],"#FFCE3A",20)]:
            tk.Label(self.slide_area,text=txt,bg="#091B2B",fg=color,font=("Segoe UI",size,"bold"),wraplength=450).pack(pady=8)
        tk.Label(self.slide_area,text=f'ACTIVE SINCE: {a["since"]}',bg="#091B2B",fg=WHITE,font=("Segoe UI",12)).pack()
        try:
            sec=max(0,int((datetime.now()-parse_time(a["since"])).total_seconds()))
            elapsed=f"{sec//3600:02d}:{(sec%3600)//60:02d}:{sec%60:02d}"
        except Exception: elapsed="--:--:--"
        tk.Label(self.slide_area,text=f"ELAPSED: {elapsed}",bg="#091B2B",fg=AMBER,font=("Segoe UI",16,"bold")).pack(pady=10)
        if not a["online"]: tk.Label(self.slide_area,text="⚠ COMMUNICATION LOST",bg="#091B2B",fg=RED).pack()
        self.slide_counter.config(text=f"SLIDE {self.slide_index+1} / {len(active)}")

    def rotate_slides(self):
        self.render_slide()
        self.slide_index=(self.slide_index+1)%len(self.active_rows) if self.active_rows else 0
        self.root.after(SLIDE_SECONDS*1000,self.rotate_slides)

    def update_clock(self):
        self.clock_label.config(text=datetime.now().strftime("%A  |  %d-%m-%Y  |  %I:%M:%S %p"))
        self.root.after(500,self.update_clock)

    # Machine list management
    def refresh_machine_table(self,select_id=None):
        items=load_machines()
        self.fill_tree(self.machine_tree,[(m.get("line",""),m.get("number",""),m.get("name",""),m.get("make",""),m.get("details",""),m.get("machine_id",""),"YES" if m.get("enabled",True) else "NO") for m in items])
        self.refresh_machine_choices()
        self.refresh_machine_history_choices()
        if select_id:
            for iid in self.machine_tree.get_children():
                if self.machine_tree.item(iid,"values")[5]==select_id:
                    self.machine_tree.selection_set(iid); self.machine_tree.focus(iid); break

    def refresh_machine_choices(self):
        if not hasattr(self,"machine_choice"): return
        items=[m for m in load_machines() if m.get("enabled",True)]
        self.machine_choice["values"]=[f'{m["line"]} | {m["number"]} | {m["name"]} | {m["machine_id"]}' for m in items]

    def set_machine_choice(self,mid):
        for i,value in enumerate(self.machine_choice["values"]):
            if value.endswith("| "+mid):
                self.machine_choice.current(i); return

    def on_slip_machine_select(self,event=None):
        value=self.machine_choice.get()
        mid=value.split("|")[-1].strip()
        m=find_machine(mid)
        if m:
            self.slip_machine_id.set(mid)
            for k,v in [("line",m["line"]),("number",m["number"]),("name",m["name"])]:
                self.slip_vars[k].set(v)

    def select_machine(self,event=None):
        sel=self.machine_tree.selection()
        if not sel: return
        vals=self.machine_tree.item(sel[0],"values")
        for k,v in zip(("line","number","name","make","details","machine_id"),vals[:6]):
            self.machine_vars[k].set(v)

    def clear_machine_fields(self):
        for v in self.machine_vars.values(): v.set("")
        self.machine_tree.selection_remove(self.machine_tree.selection())

    def add_machine(self):
        m={k:v.get().strip() for k,v in self.machine_vars.items()}
        if not all(m[k] for k in ("line","number","name","machine_id")):
            messagebox.showwarning("Missing data","Please fill Line, Machine number, Machine name and Machine ID. Make and details are optional."); return
        items=load_machines()
        if any(x["machine_id"].casefold()==m["machine_id"].casefold() for x in items):
            messagebox.showerror("Duplicate ID","Machine ID must be unique."); return
        if any(x["line"].casefold()==m["line"].casefold() and x["number"].casefold()==m["number"].casefold() for x in items):
            messagebox.showerror("Duplicate machine","That line + machine number already exists."); return
        m["enabled"]=True; items.append(m); save_machines(items); ensure_machine_row(m)
        self.refresh_machine_table(m["machine_id"]); self.clear_machine_fields()
        messagebox.showinfo("Saved","Machine added to machine_list/machines.json.")

    def update_machine(self):
        sel=self.machine_tree.selection()
        if not sel: messagebox.showwarning("Select machine","Select a machine row first."); return
        old=self.machine_tree.item(sel[0],"values")[5]
        m={k:v.get().strip() for k,v in self.machine_vars.items()}
        if not all(m[k] for k in ("line","number","name","machine_id")):
            messagebox.showwarning("Missing data","Please fill Line, Machine number, Machine name and Machine ID. Make and details are optional."); return
        items=load_machines()
        if any(x["machine_id"]!=old and x["machine_id"].casefold()==m["machine_id"].casefold() for x in items):
            messagebox.showerror("Duplicate ID","Machine ID must be unique."); return
        for i,x in enumerate(items):
            if x["machine_id"]==old: items[i]={**m,"enabled":True}; break
        save_machines(items); ensure_machine_row({**m})
        self.refresh_machine_table(m["machine_id"]); messagebox.showinfo("Updated","Machine list updated. ESP32 identity must match the new values.")

    def remove_machine(self):
        sel=self.machine_tree.selection()
        if not sel: messagebox.showwarning("Select machine","Select a machine row first."); return
        vals=self.machine_tree.item(sel[0],"values"); mid=vals[5]
        if not messagebox.askyesno("Confirm remove",f"Remove {vals[2]} ({mid}) from the available machine list? Incoming data will be rejected."): return
        save_machines([m for m in load_machines() if m["machine_id"]!=mid])
        self.refresh_machine_table(); self.clear_machine_fields()

    def search_machine(self):
        q=self.machine_vars["name"].get().strip() or self.machine_vars["number"].get().strip() or self.machine_vars["machine_id"].get().strip() or self.machine_vars["line"].get().strip()
        if not q: q=tk.simpledialog.askstring("Search","Enter line, machine number, name, or ID:")
        if not q: return
        matches=[m for m in load_machines() if q.casefold() in " ".join(str(m.get(k,"")) for k in ("line","number","name","make","details","machine_id")).casefold()]
        self.fill_tree(self.machine_tree,[(m.get("line",""),m.get("number",""),m.get("name",""),m.get("make",""),m.get("details",""),m.get("machine_id",""),"YES" if m.get("enabled",True) else "NO") for m in matches])
        if not matches: messagebox.showinfo("Search","No matching machine found.")

    def print_machine_list(self):
        """Create a formatted A4 Excel workbook and print it when Excel automation is available."""
        items=load_machines()
        if not items:
            messagebox.showinfo("Machine list", "No registered machines to print.")
            return
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill, Border, Side, Alignment
            from openpyxl.worksheet.table import Table, TableStyleInfo
            from openpyxl.utils import get_column_letter
        except ImportError:
            messagebox.showerror("Excel library missing",
                "A4 Excel print needs openpyxl. Install it using:\npython -m pip install openpyxl")
            return

        out_path = MACHINE_DIR / "Machine_List_A4.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.title = "Machine List"
        headers = ["Sr. No.", "Line", "Machine No.", "Machine Name", "Machine Make", "Machine Details", "ESP32 Machine ID", "Enabled"]
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
        title = ws.cell(1, 1, "MACHINE REGISTER")
        title.font = Font(name="Arial", size=16, bold=True, color="FFFFFF")
        title.fill = PatternFill("solid", fgColor="16466A")
        title.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 30
        ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(headers))
        stamp = ws.cell(2, 1, f"Printed on: {datetime.now().strftime('%d-%m-%Y %I:%M %p')}    |    Total machines: {len(items)}")
        stamp.font = Font(name="Arial", size=9, italic=True, color="334155")
        stamp.alignment = Alignment(horizontal="right")
        for col, header in enumerate(headers, 1):
            c = ws.cell(4, col, header)
            c.font = Font(name="Arial", size=9, bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="2878A8")
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        thin = Side(style="thin", color="9AAFC0")
        for idx, m in enumerate(items, 1):
            values = [idx, m.get("line", ""), m.get("number", ""), m.get("name", ""),
                      m.get("make", ""), m.get("details", ""), m.get("machine_id", ""),
                      "YES" if m.get("enabled", True) else "NO"]
            row_num = idx + 4
            for col, value in enumerate(values, 1):
                c = ws.cell(row_num, col, value)
                c.font = Font(name="Arial", size=8, color="172B3A")
                c.alignment = Alignment(vertical="top", horizontal="center" if col in (1, 8) else "left", wrap_text=True)
                c.border = Border(left=thin, right=thin, top=thin, bottom=thin)
                if idx % 2 == 0:
                    c.fill = PatternFill("solid", fgColor="EAF2F8")
            ws.row_dimensions[row_num].height = 32 if m.get("details") else 23
        widths = [7, 13, 13, 22, 19, 32, 25, 9]
        for col, width in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(col)].width = width
        ws.row_dimensions[4].height = 32
        ws.freeze_panes = "A5"
        ws.auto_filter.ref = f"A4:H{len(items)+4}"
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.page_setup.orientation = "landscape"
        ws.page_setup.paperSize = ws.PAPERSIZE_A4
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.page_margins.left = 0.25
        ws.page_margins.right = 0.25
        ws.page_margins.top = 0.45
        ws.page_margins.bottom = 0.45
        ws.page_margins.header = 0.2
        ws.page_margins.footer = 0.2
        ws.print_title_rows = "1:4"
        ws.print_area = f"A1:H{len(items)+4}"
        ws.oddFooter.center.text = "Page &P of &N"
        ws.oddFooter.right.text = "IoT Breakdown Monitoring System"
        try:
            wb.save(out_path)
        except Exception as exc:
            messagebox.showerror("Excel export failed", f"Could not save Excel file:\n{exc}")
            return

        # Print immediately on Windows when Microsoft Excel automation is installed.
        printed = False
        if os.name == "nt":
            try:
                import win32com.client
                excel = win32com.client.DispatchEx("Excel.Application")
                excel.Visible = False
                book = excel.Workbooks.Open(str(out_path.resolve()))
                book.Worksheets(1).PrintOut()
                book.Close(SaveChanges=False)
                excel.Quit()
                printed = True
            except Exception:
                printed = False
        if printed:
            messagebox.showinfo("Print", f"Machine list sent to the default printer.\nExcel file saved at:\n{out_path}")
        else:
            try:
                if os.name == "nt":
                    os.startfile(str(out_path))
                else:
                    import webbrowser
                    webbrowser.open(out_path.resolve().as_uri())
            except Exception:
                pass
            messagebox.showinfo("A4 Excel ready", f"Formatted A4 Excel file created:\n{out_path}\n\nOpen it in Excel and press Ctrl+P to print. If you want direct printing on Windows, install pywin32: python -m pip install pywin32")

    def show_all_machines(self):
        self.refresh_machine_table()
        messagebox.showinfo("Machine list",f"Total registered machines: {len(load_machines())}")

    # Breakdown slips
    def clear_slip(self,keep_alarm=False):
        for k,v in self.slip_vars.items(): v.set("")
        self.slip_vars["status"].set("OPEN")
        self.slip_vars["start"].set(timestamp())
        self.slip_vars["end"].set("")
        self.slip_machine_id.set("")
        self.current_slip_id=None
        if not keep_alarm: self.current_alarm_prefill=None
        if hasattr(self,"machine_choice"): self.machine_choice.set("")
        self.slip_index=0

    def use_current_alarm(self):
        if not self.current_alarm_prefill:
            messagebox.showinfo("No current alarm","No newly received active alarm is waiting. You can create a manual slip."); return
        for k,v in self.current_alarm_prefill.items():
            if k in self.slip_vars: self.slip_vars[k].set(v)
        self.slip_machine_id.set(self.current_alarm_prefill["machine_id"])
        self.set_machine_choice(self.slip_machine_id.get())

    def save_slip(self, publish=False, force_new=False):
        """
        Send creates one new active slip.
        Save is ONLY allowed to close an already-sent OPEN / IN PROGRESS / ON HOLD slip.
        A closed slip cannot be saved again, preventing duplicate solved entries.
        """
        active_statuses = {"OPEN", "IN PROGRESS", "ON HOLD"}

        if publish:
            # Resolve the selected machine from the configured machine list.
            mid = self.slip_machine_id.get().strip()
            machine = find_machine(mid)
            if not machine:
                vals = [self.slip_vars[k].get().strip() for k in ("line", "number", "name")]
                machine = next((m for m in load_machines()
                                if [m.get("line", ""), m.get("number", ""), m.get("name", "")] == vals), None)
                if machine:
                    mid = machine["machine_id"]
            if not machine:
                messagebox.showerror(
                    "Machine mismatch",
                    "Choose a machine from the available machine list. Line, number and name must match exactly."
                )
                return

            self.slip_machine_id.set(mid)
            for key, value in (("line", machine["line"]), ("number", machine["number"]), ("name", machine["name"])):
                self.slip_vars[key].set(value)

            description = self.slip_vars["description"].get().strip()
            if not description:
                messagebox.showwarning("Missing description", "Enter a breakdown description before Send.")
                return

            status = re.sub(r"\s+", " ", self.slip_vars["status"].get().strip().upper().replace("_", " "))
            if status not in active_statuses:
                status = "OPEN"

            start_value = self.slip_vars["start"].get().strip() or timestamp()
            try:
                parse_time(start_value)
            except ValueError:
                messagebox.showerror("Date/time format", "Breakdown start time must be YYYY-MM-DD HH:MM:SS")
                return

            # Unique microsecond ID: sending another breakdown never overwrites an earlier one.
            created = timestamp()
            slip_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            data = {key: var.get().strip() for key, var in self.slip_vars.items()}
            data.update({
                "machine_id": mid,
                "slip_id": slip_id,
                "created_at": created,
                "status": status,
                "sent_to_dashboard": True,
                "start": start_value,
                "end": "",
                "action": "",
                "alarm_active_time": start_value,
                "solved_time": "",
                "duration": ""
            })
            path = SLIP_DIR / (safe_id(slip_id) + ".json")
            try:
                path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception as exc:
                messagebox.showerror("Save failed", f"Could not create breakdown slip:\n{exc}")
                return

            self.current_slip_id = slip_id
            self.slips = list_slips()
            self.refresh_slip_list()
            self.update_dashboard()
            self.nb.select(self.page1)
            messagebox.showinfo(
                "Sent to Active Dashboard",
                "Breakdown slip is now active. It will remain in the Active Alarm Live List until you select this slip, enter Action Taken, and press Save."
            )
            return

        # Normal Save is a close action, never a create operation.
        sid = getattr(self, "current_slip_id", None)
        if not sid:
            messagebox.showwarning(
                "Select active breakdown",
                "Repeated or new CLOSED entries are not allowed. Select an existing OPEN / IN PROGRESS / ON HOLD slip from Saved Breakdown Slips, enter Action Taken, then press Save."
            )
            return

        path = SLIP_DIR / (safe_id(sid) + ".json")
        if not path.exists():
            messagebox.showerror("Slip not found", "The selected breakdown slip no longer exists. Refresh the saved slips list.")
            self.current_slip_id = None
            return

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            messagebox.showerror("Read failed", f"Could not read selected breakdown slip:\n{exc}")
            return

        old_status = re.sub(r"\s+", " ", str(data.get("status", "")).strip().upper().replace("_", " "))
        if not data.get("sent_to_dashboard") or old_status not in active_statuses:
            messagebox.showwarning(
                "Already closed / not active",
                "Only a slip previously sent with status OPEN, IN PROGRESS or ON HOLD can be closed. This slip will not be saved again."
            )
            self.current_slip_id = None
            return

        action = self.slip_vars["action"].get().strip()
        if not action:
            messagebox.showwarning("Action Taken required", "Enter Action Taken before closing the breakdown.")
            return

        # Preserve the original machine, description and alarm start time.
        solved_at = timestamp()
        active_at = (data.get("alarm_active_time") or data.get("start")
                     or data.get("created_at") or solved_at)
        try:
            parse_time(active_at)
        except ValueError:
            active_at = solved_at

        data["action"] = action
        data["status"] = "CLOSED"
        data["end"] = solved_at
        data["solved_time"] = solved_at
        data["alarm_active_time"] = active_at
        data["duration"] = format_duration(active_at, solved_at)
        data["sent_to_dashboard"] = True
        data["closed_once"] = True

        try:
            temp_path = path.with_suffix(".tmp")
            temp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            temp_path.replace(path)
        except Exception as exc:
            messagebox.showerror("Save failed", f"Could not close breakdown slip:\n{exc}")
            return

        self.current_slip_id = None
        self.slips = list_slips()
        self.refresh_slip_list()
        self.update_dashboard()
        messagebox.showinfo(
            "Breakdown Solved",
            f"Breakdown closed successfully.\nAlarm Active Time: {active_at}\nSolved Time: {solved_at}\nDuration: {data['duration']}\n\nThis slip is now in Saved Breakdown Slips and Solved Alarm History. It cannot be saved a second time."
        )

    def send_slip(self):
        # Every Send creates a new independent slip; existing active slips are never overwritten.
        self.save_slip(publish=True, force_new=True)

    def sync_page1_completed_slips(self):
        """Import Page 1 solved alarm-history rows into the saved-slip register once."""
        try:
            with db_lock, connect_db() as con:
                rows = con.execute("""SELECT a.id, a.machine_id, a.alarm_name, a.active_time,
                    a.solved_time, m.line, m.machine_number, m.machine_name
                    FROM alarm_history a JOIN machine_status m ON m.machine_id=a.machine_id
                    WHERE a.status='SOLVED' AND a.solved_time IS NOT NULL ORDER BY a.id DESC""").fetchall()
            existing = {str(x.get("source_alarm_id", "")) for x in list_slips() if x.get("source_alarm_id")}
            tombstone_path = SLIP_DIR / "_deleted_page1_alarm_ids.json"
            try:
                deleted_ids = set(str(x) for x in json.loads(tombstone_path.read_text(encoding="utf-8"))) if tombstone_path.exists() else set()
            except Exception:
                deleted_ids = set()
            for row in rows:
                alarm_id = str(row["id"])
                if alarm_id in existing or alarm_id in deleted_ids:
                    continue
                # Keep the machine identity aligned with the current configured machine list.
                machine = find_machine(row["machine_id"])
                if not machine:
                    continue
                if not machine_matches({"machine_id": row["machine_id"], "line": row["line"],
                    "number": row["machine_number"], "name": row["machine_name"]}, machine):
                    continue
                slip_id = f"PAGE1_SOLVED_{alarm_id}"
                data = {
                    "slip_id": slip_id, "machine_id": row["machine_id"],
                    "line": row["line"] or "", "number": row["machine_number"] or "",
                    "name": row["machine_name"] or "", "description": row["alarm_name"] or "Solved alarm",
                    "action": "Alarm cleared; automatically imported from Page 1 solved alarm history.",
                    "status": "CLOSED", "start": row["active_time"] or "", "end": row["solved_time"] or "",
                    "alarm_active_time": row["active_time"] or "", "solved_time": row["solved_time"] or "",
                    "duration": format_duration(row["active_time"] or "", row["solved_time"] or ""),
                    "source_alarm": row["alarm_name"] or "", "source_alarm_id": alarm_id,
                    "created_at": row["solved_time"] or timestamp(), "sent_to_dashboard": False,
                    "imported_from_page1": True
                }
                target = SLIP_DIR / (safe_id(slip_id) + ".json")
                if not target.exists():
                    target.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            print(f"Could not sync Page 1 completed slips: {exc}")

    def refresh_slip_list(self):
        if not hasattr(self,"slip_tree"): return
        self.sync_page1_completed_slips()
        self.slips=list_slips()
        rows=[]
        for s in self.slips:
            active_time=s.get("alarm_active_time") or s.get("start") or s.get("created_at", "")
            solved_time=s.get("solved_time") or s.get("end", "")
            duration=s.get("duration") or (format_duration(active_time, solved_time) if solved_time else "")
            rows.append((s.get("slip_id",""),s.get("line",""),s.get("number",""),s.get("name",""),
                         s.get("description",""),s.get("status",""),active_time,solved_time,duration))
        self.fill_tree(self.slip_tree,rows)
        if hasattr(self,"slip_count_label"):
            completed=sum(1 for x in self.slips if str(x.get("status","")).strip().upper() in {"CLOSED","COMPLETED","SOLVED"})
            self.slip_count_label.config(text=f"Total saved slips: {len(self.slips)} | Completed: {completed}")

    def print_saved_slips(self):
        """Export the complete saved breakdown-slip register to a formatted A4 Excel workbook."""
        slips=list_slips()
        if not slips:
            messagebox.showinfo("Saved slips", "No saved breakdown slips are available to print.")
            return
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill, Border, Side, Alignment
            from openpyxl.utils import get_column_letter
        except ImportError:
            messagebox.showerror("Excel library missing", "Install openpyxl using:\npython -m pip install openpyxl")
            return
        headers=["Sr. No.","Slip ID","Line","Machine No.","Machine Name","Breakdown Description",
                 "Action Taken","Status","Alarm Active Time","Solved Time","Duration"]
        out_path=SLIP_DIR/"Saved_Breakdown_Slips_A4.xlsx"
        wb=Workbook(); ws=wb.active; ws.title="Breakdown Slips"
        ws.merge_cells(start_row=1,start_column=1,end_row=1,end_column=len(headers))
        c=ws.cell(1,1,"BREAKDOWN SLIP REGISTER — COMPLETE HISTORY")
        c.font=Font(name="Arial",size=16,bold=True,color="FFFFFF")
        c.fill=PatternFill("solid",fgColor="16466A"); c.alignment=Alignment(horizontal="center",vertical="center")
        ws.row_dimensions[1].height=30
        ws.merge_cells(start_row=2,start_column=1,end_row=2,end_column=len(headers))
        c=ws.cell(2,1,f"Generated: {timestamp()}    |    Total slips: {len(slips)}    |    Closed: {sum(1 for x in slips if str(x.get('status','')).upper() in {'CLOSED','SOLVED','COMPLETED'})}")
        c.font=Font(name="Arial",size=9,italic=True,color="334155"); c.alignment=Alignment(horizontal="right")
        header_row=4
        for col,h in enumerate(headers,1):
            c=ws.cell(header_row,col,h); c.font=Font(name="Arial",size=9,bold=True,color="FFFFFF")
            c.fill=PatternFill("solid",fgColor="2878A8"); c.alignment=Alignment(horizontal="center",vertical="center",wrap_text=True)
        thin=Side(style="thin",color="9AAFC0")
        for idx,slip in enumerate(slips,1):
            active=slip.get("alarm_active_time") or slip.get("start") or slip.get("created_at","")
            solved=slip.get("solved_time") or slip.get("end","")
            duration=slip.get("duration") or (format_duration(active,solved) if solved else "ACTIVE")
            vals=[idx,slip.get("slip_id",""),slip.get("line",""),slip.get("number",""),slip.get("name",""),
                  slip.get("description",""),slip.get("action",""),slip.get("status",""),active,solved,duration]
            r=header_row+idx
            for col,val in enumerate(vals,1):
                c=ws.cell(r,col,val); c.font=Font(name="Arial",size=8,color="172B3A")
                c.alignment=Alignment(vertical="top",horizontal="center" if col in (1,8,11) else "left",wrap_text=True)
                c.border=Border(left=thin,right=thin,top=thin,bottom=thin)
                if idx%2==0: c.fill=PatternFill("solid",fgColor="EAF2F8")
            ws.row_dimensions[r].height=38
        widths=[7,22,12,13,22,34,34,13,21,21,15]
        for col,width in enumerate(widths,1): ws.column_dimensions[get_column_letter(col)].width=width
        ws.row_dimensions[header_row].height=32; ws.freeze_panes="A5"
        ws.auto_filter.ref=f"A4:K{len(slips)+4}"
        ws.sheet_properties.pageSetUpPr.fitToPage=True
        ws.page_setup.orientation="landscape"; ws.page_setup.paperSize=ws.PAPERSIZE_A4
        ws.page_setup.fitToWidth=1; ws.page_setup.fitToHeight=0
        ws.page_margins.left=0.2; ws.page_margins.right=0.2; ws.page_margins.top=0.4; ws.page_margins.bottom=0.4
        ws.print_title_rows="1:4"; ws.print_area=f"A1:K{len(slips)+4}"
        ws.oddFooter.center.text="Page &P of &N"; ws.oddFooter.right.text="IoT Breakdown Monitoring System"
        try: wb.save(out_path)
        except Exception as exc:
            messagebox.showerror("Excel export failed",f"Could not save Excel file:\n{exc}"); return
        try:
            if os.name=="nt": os.startfile(str(out_path))
            else:
                import webbrowser; webbrowser.open(out_path.resolve().as_uri())
        except Exception: pass
        messagebox.showinfo("A4 Excel ready",f"Complete saved breakdown-slip register exported to:\n{out_path}\n\nOpen in Excel and press Ctrl+P. Page setup is A4 landscape.")

    def delete_selected_slip(self):
        """Delete the selected saved slip after explicit confirmation."""
        selected = self.slip_tree.selection() if hasattr(self, "slip_tree") else ()
        if not selected:
            messagebox.showwarning("Select slip", "Please select a saved breakdown slip from the list first.")
            return
        values = self.slip_tree.item(selected[0], "values")
        sid = str(values[0]) if values else ""
        if not sid:
            messagebox.showerror("Delete slip", "Could not identify the selected slip.")
            return
        path = SLIP_DIR / (safe_id(sid) + ".json")
        if not path.exists():
            messagebox.showerror("Delete slip", "The selected slip file was not found.")
            self.refresh_slip_list()
            return
        if not messagebox.askyesno("Confirm delete", f"Permanently delete breakdown slip {sid}?\nThis action cannot be undone."):
            return
        try:
            try:
                selected_data = json.loads(path.read_text(encoding="utf-8"))
                source_alarm_id = selected_data.get("source_alarm_id")
                if source_alarm_id:
                    tombstone_path = SLIP_DIR / "_deleted_page1_alarm_ids.json"
                    try:
                        deleted_ids = set(str(x) for x in json.loads(tombstone_path.read_text(encoding="utf-8"))) if tombstone_path.exists() else set()
                    except Exception:
                        deleted_ids = set()
                    deleted_ids.add(str(source_alarm_id))
                    tombstone_path.write_text(json.dumps(sorted(deleted_ids), indent=2), encoding="utf-8")
            except Exception:
                pass
            path.unlink()
        except OSError as exc:
            messagebox.showerror("Delete failed", f"Could not delete slip:\n{exc}")
            return
        self.latest_sent_slip = next((x for x in list_slips() if dashboard_eligible(x)), None)
        self.update_sent_slip_banner()
        self.refresh_slip_list()
        self.clear_slip()
        messagebox.showinfo("Deleted", f"Breakdown slip {sid} deleted successfully.")

    def view_slips(self):
        self.refresh_slip_list()
        if self.slips:
            self.slip_index=0; self.load_slip(self.slips[0])
        else: messagebox.showinfo("Saved slips","No breakdown slips have been saved yet.")

    def select_slip_from_tree(self,event=None):
        sel=self.slip_tree.selection()
        if not sel: return
        sid=self.slip_tree.item(sel[0],"values")[0]
        self.slips=list_slips()
        for i,s in enumerate(self.slips):
            if s.get("slip_id")==sid: self.slip_index=i; self.load_slip(s); break

    def load_slip(self,s):
        self.current_slip_id=s.get("slip_id")
        for k in self.slip_vars:
            self.slip_vars[k].set(s.get(k,""))
        self.slip_machine_id.set(s.get("machine_id",""))
        self.set_machine_choice(s.get("machine_id",""))

    def previous_slip(self):
        self.slips=list_slips()
        if not self.slips: messagebox.showinfo("Slips","No saved slips."); return
        self.slip_index=(self.slip_index+1)%len(self.slips)
        self.load_slip(self.slips[self.slip_index])

    def next_slip(self):
        self.slips=list_slips()
        if not self.slips: messagebox.showinfo("Slips","No saved slips."); return
        self.slip_index=(self.slip_index-1)%len(self.slips)
        self.load_slip(self.slips[self.slip_index])

if __name__=="__main__":
    init_db()
    threading.Thread(target=lambda: app.run(host=HOST,port=PORT,debug=False,use_reloader=False,threaded=True),daemon=True).start()
    root=tk.Tk()
    # Used only by the optional machine search prompt.
    import tkinter.simpledialog
    Dashboard(root)
    root.mainloop()
