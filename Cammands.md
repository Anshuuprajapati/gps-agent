python reset_session.py MH12AB1234 --full








curl -X POST http://127.0.0.1:8000/trigger-outage/MH12AB1234
curl -X POST http://127.0.0.1:8000/trigger-outage/MH14CD5678
curl -X POST http://127.0.0.1:8000/trigger-outage/MH16EF9012




uvicorn main:app --reload --port 8000