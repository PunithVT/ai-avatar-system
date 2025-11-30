#!/bin/bash

set -e

echo "🚀 Deploying AI Avatar System to AWS..."

# Check if environment is specified
ENVIRONMENT=${1:-production}
echo "Environment: $ENVIRONMENT"

# Check AWS credentials
if [ -z "$AWS_ACCESS_KEY_ID" ] || [ -z "$AWS_SECRET_ACCESS_KEY" ]; then
    echo "❌ AWS credentials not set. Please set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY"
    exit 1
fi

# Load environment variables
if [ -f .env ]; then
    echo "📝 Loading environment variables..."
    export $(cat .env | grep -v '^#' | xargs)
fi

# Build Docker images
echo "📦 Building Docker images..."
docker-compose build --no-cache

# Tag and push images to ECR
AWS_REGION=${AWS_REGION:-us-east-1}
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_REGISTRY="$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"

echo "🔐 Logging in to ECR..."
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $ECR_REGISTRY

# Create ECR repositories if they don't exist
echo "📦 Creating ECR repositories..."
aws ecr create-repository --repository-name avatar-backend --region $AWS_REGION 2>/dev/null || true
aws ecr create-repository --repository-name avatar-frontend --region $AWS_REGION 2>/dev/null || true

# Tag images
docker tag avatar-backend:latest $ECR_REGISTRY/avatar-backend:latest
docker tag avatar-frontend:latest $ECR_REGISTRY/avatar-frontend:latest

# Push images
echo "⬆️  Pushing images to ECR..."
docker push $ECR_REGISTRY/avatar-backend:latest
docker push $ECR_REGISTRY/avatar-frontend:latest

# Deploy infrastructure with Terraform
echo "🏗️  Deploying AWS infrastructure..."
cd infrastructure

# Initialize Terraform
terraform init

# Create terraform.tfvars if it doesn't exist
if [ ! -f terraform.tfvars ]; then
    cat > terraform.tfvars <<EOF
aws_region      = "$AWS_REGION"
environment     = "$ENVIRONMENT"
s3_bucket_name  = "$S3_BUCKET_NAME"
db_password     = "$DATABASE_PASSWORD"
EOF
fi

# Plan and apply
terraform plan -out=tfplan
terraform apply tfplan

# Get outputs
S3_BUCKET=$(terraform output -raw s3_bucket_name)
CLOUDFRONT_DOMAIN=$(terraform output -raw cloudfront_domain)
DB_ENDPOINT=$(terraform output -raw db_endpoint)
REDIS_ENDPOINT=$(terraform output -raw redis_endpoint)

cd ..

echo "✅ Infrastructure deployed successfully!"
echo ""
echo "📊 Deployment Summary:"
echo "  S3 Bucket: $S3_BUCKET"
echo "  CloudFront: https://$CLOUDFRONT_DOMAIN"
echo "  Database: $DB_ENDPOINT"
echo "  Redis: $REDIS_ENDPOINT"
echo ""
echo "🔗 Next steps:"
echo "  1. Update .env with the new endpoints"
echo "  2. Deploy ECS services: ./deploy-ecs.sh"
echo "  3. Access your application at https://$CLOUDFRONT_DOMAIN"
