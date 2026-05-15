from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..app.database import get_db
from ..app import models, schemas


router = APIRouter(
    prefix="/users",
    tags=["Users"]
)


#Get users from DB
@router.get("/", response_model=list[schemas.UserResponse])
def get_users(db: Session = Depends(get_db)):
    users = db.query(models.User).all()

    return users


#Create a new user to DB
@router.post("/", response_model=schemas.UserResponse)
def create_user(user: schemas.UserCreate, db: Session = Depends(get_db)):
    db_user = models.User(
        name=user.name,
        mail=user.mail
    )

    db.add(db_user)
    db.commit()
    db.refresh(db_user)

    return db_user


