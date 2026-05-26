import os
os.environ["DB_PATH"] = "/data/state.db"
import db
from datetime import date
db.reset_flags(date.today())
print("Флаги сброшены:", db.get_flags(date.today()))