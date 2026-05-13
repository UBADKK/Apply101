import requests
from fastapi import FastAPI
from bs4 import BeautifulSoup


app = FastAPI()

keywords = ["junior", "developer"]


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text(separator="\n", strip=True)


@app.get("/")
def read_root():
    url = "https://www.arbeitnow.com/api/job-board-api"
    response = requests.get(url)
    data = response.json()
    jobs = data["data"]

    clean_jobs = []

    for job in jobs[:5]:
        clean_description = html_to_text(job["description"])

        clean_job = {
            "title": job["title"],
            "company_name": job["company_name"],
            "location": job["location"],
            "url": job["url"],
            "description_text":clean_description,
        }

        clean_jobs.append(clean_job)
    print(clean_jobs)
    return clean_jobs

@app.post("/kek")
def create_job():
    return {"message": "Job created"}

