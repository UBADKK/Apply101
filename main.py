import requests
from fastapi import FastAPI


app = FastAPI()

@app.get("/")
def read_root():
    url = "https://www.arbeitnow.com/api/job-board-api"
    response = requests.get(url).json()
    return response

@app.post("/kek")
def create_job():
    return {"message": "Job created"}
