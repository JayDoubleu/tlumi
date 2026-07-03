"""Simple FastAPI app to deploy to Azure Container Apps."""

from fastapi import FastAPI

app = FastAPI()


@app.get("/")
def root():
    return {"message": "Hello from tlumi on Azure Container Apps!"}


@app.get("/health")
def health():
    return {"status": "healthy"}
