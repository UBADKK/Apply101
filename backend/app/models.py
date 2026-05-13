class Job(BaseModel):
__init__(self, id, title, description, company, location, url, created_at):
    self.id = id
    self.title = title
    self.description = description
    self.company = company
    self.location = location
    self.url = url
    self.created_at = created_at

class Job(BaseModel):
    id: int
    title: str
    description: str
    company: str
    location: str
    url: str
    created_at: str