from fastapi import APIRouter, Depends, HTTPException
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


# Create a new user to DB
@router.post("/", response_model=schemas.UserResponse)
def create_user(user: schemas.UserCreate, db: Session = Depends(get_db)):
    existing_user = db.query(models.User).filter(
        models.User.mail == user.mail
    ).first()

    if existing_user:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "ERR_USER_MAIL_ALREADY_EXISTS",
                "message": f"User with mail {user.mail} already exists.",
                "user_id": existing_user.user_id
            }
        )

    db_user = models.User(
        name=user.name,
        mail=user.mail,
        skills=user.skills,
        experience_years=user.experience_years,
        major=user.major,
        master=user.master,
        phd=user.phd,
        abitur=user.abitur
    )

    db.add(db_user)
    db.commit()
    db.refresh(db_user)

    return db_user

# Delete user and related data from DB
@router.delete("/{user_id}")
def delete_user(user_id: int, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(
        models.User.user_id == user_id
    ).first()

    if not user:
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "ERR_USER_NOT_FOUND",
                "message": f"User with id {user_id} was not found."
            }
        )

    profiles = db.query(models.CandidateProfile).filter(
        models.CandidateProfile.user_id == user_id
    ).all()

    profile_ids = [profile.profile_id for profile in profiles]

    deleted_matches_count = 0
    deleted_profile_analyses_count = 0
    deleted_profiles_count = 0

    if profile_ids:
        deleted_matches_count = db.query(models.JobMatch).filter(
            models.JobMatch.profile_id.in_(profile_ids)
        ).delete(synchronize_session=False)

        deleted_profile_analyses_count = db.query(models.ProfileAnalysis).filter(
            models.ProfileAnalysis.profile_id.in_(profile_ids)
        ).delete(synchronize_session=False)

        deleted_profiles_count = db.query(models.CandidateProfile).filter(
            models.CandidateProfile.profile_id.in_(profile_ids)
        ).delete(synchronize_session=False)

    deleted_languages_count = db.query(models.UserLanguage).filter(
        models.UserLanguage.user_id == user_id
    ).delete(synchronize_session=False)

    db.delete(user)
    db.commit()

    return {
        "status": "deleted",
        "user_id": user_id,
        "deleted_profiles_count": deleted_profiles_count,
        "deleted_profile_analyses_count": deleted_profile_analyses_count,
        "deleted_matches_count": deleted_matches_count,
        "deleted_languages_count": deleted_languages_count
    }