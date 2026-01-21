from fastapi import FastAPI
from app.core.config import settings
from app.api.v1.endpoints import verify, auth, users, tokens, estates, billings, landlords

app = FastAPI(title=settings.PROJECT_NAME)

app.include_router(auth.router, prefix=f"{settings.API_V1_STR}/auth", tags=["auth"])
app.include_router(verify.router, prefix=f"{settings.API_V1_STR}/verify", tags=["profiling"])
app.include_router(users.router, prefix=f"{settings.API_V1_STR}/users") # Tags are defined in router now
app.include_router(tokens.router, prefix=f"{settings.API_V1_STR}/tokens") # Tags defined in router
app.include_router(estates.router, prefix=f"{settings.API_V1_STR}/estates", tags=["super_admin"])
app.include_router(billings.router, prefix=f"{settings.API_V1_STR}/billings")
app.include_router(landlords.router, prefix=f"{settings.API_V1_STR}/landlords", tags=["landlords"])

@app.get("/")
async def root():
    return {"message": "Welcome to Residence SaaS API"}
