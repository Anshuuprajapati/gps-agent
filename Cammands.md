python reset_session.py MH12AB1234 --full








curl -X POST http://127.0.0.1:8000/trigger-outage/MH12AB1234
curl -X POST http://127.0.0.1:8000/trigger-outage/MH14CD5678
curl -X POST http://127.0.0.1:8000/trigger-outage/MH16EF9012




uvicorn main:app --reload --port 8000





phone_number,vehicle_no,last_location,timestamp,gpstime,main_powervoltage,ismainpoerconnected,gpsStatus,driver_name,driver_phone,current_location,vehicle_state,current_state,handler,extracted_appointment_date,extracted_service_location,root_cause,physical_damage,contact_person,contact_number,service_date,service_time,ticket_id,engineer_id

918882374849,MH12AB1234,Pune,2026-06-20 10:00:00,20 June 2026 10:00,10.8,1,0,Salman,9105853736,,,START,OWNER,,,,,,,,,,

918882374849,MH14CD5678,Mumbai,2026-06-19 18:00:00,19 June 2026 18:00,12.4,0,0,Ravi Yadav,9988776655,,,START,OWNER,,,,,,,,,,

918882374849,MH16EF9012,Nagpur,2026-06-21 09:30:00,21 June 2026 09:30,12.6,1,0,Deepak Singh,9871234560,,,START,OWNER,,,,,,,,,,


