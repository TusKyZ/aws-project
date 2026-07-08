# terraform -chdir=terraform/bootstrap init -backend-config=backend.hcl
# CHANGEME: same state bucket as the main stack (see ../backend.tf).
bucket = "CHANGEME-sentinel-tfstate"
region = "us-east-1"
