Skill Arena Hub - Deployment

Render settings:
Build Command: pip install -r requirements.txt
Start Command: gunicorn skill_arena:app --bind 0.0.0.0:$PORT

Required environment variables:
ADMIN_USER
ADMIN_PASSWORD
SKILL_ARENA_SECRET
SKILL_ARENA_DB (optional; defaults to skill_arena.db)

Payments are MANUAL via NayaPay. No SafePay/automatic payment integration is used.
Users transfer the selected Pass amount to:
Wallet: NayaPay
Account Name: Muhammad Raffy Umer
Account Number: 03250150477
Then they enter the NayaPay TRX ID in the app. The request appears in Admin, where it can be approved or rejected. Approval activates the selected Pass.
