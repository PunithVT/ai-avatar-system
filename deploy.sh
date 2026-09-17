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
    # jwt_secret_key and secret_key have no defaults — Settings refuses to
    # start on anything shorter than 32 chars, so generate real ones rather
    # than letting terraform prompt for them mid-apply.
    cat > terraform.tfvars <<EOF
aws_region      = "$AWS_REGION"
environment     = "$ENVIRONMENT"
s3_bucket_name  = "$S3_BUCKET_NAME"
db_password     = "$DATABASE_PASSWORD"
jwt_secret_key  = "${JWT_SECRET_KEY:-$(openssl rand -hex 32)}"
secret_key      = "${SECRET_KEY:-$(openssl rand -hex 32)}"
anthropic_api_key = "${ANTHROPIC_API_KEY:-}"
openai_api_key  = "${OPENAI_API_KEY:-}"
EOF
    chmod 600 terraform.tfvars  # contains secrets
fi

# Plan and apply
terraform plan -out=tfplan
terraform apply tfplan

# Get outputs. The name here was "s3_bucket_name", which does not exist — the
# output is "s3_bucket" — so this step failed and left the summary blank.
S3_BUCKET=$(terraform output -raw s3_bucket)
CLOUDFRONT_DOMAIN=$(terraform output -raw cloudfront_domain)
DB_ENDPOINT=$(terraform output -raw db_endpoint)
REDIS_ENDPOINT=$(terraform output -raw redis_endpoint)
APP_URL=$(terraform output -raw application_url)
CLUSTER=$(terraform output -raw ecs_cluster_name)

cd ..

# Roll the services onto the images pushed above. Without this the script
# finished here and announced success over an empty cluster: images sat in
# ECR, the cluster ran nothing, and the load balancer had no targets — while
# RDS, ElastiCache, the ALB and the NAT gateway all billed.
echo "🚢 Rolling ECS services onto the new images..."
for svc in avatar-backend avatar-frontend avatar-worker; do
    echo "   $svc"
    aws ecs update-service \
        --cluster "$CLUSTER" \
        --service "$svc" \
        --force-new-deployment \
        --no-cli-pager >/dev/null
done

echo "⏳ Waiting for services to stabilise..."
echo "   (the backend cold start loads ~9 GB of MuseTalk weights — several minutes)"
aws ecs wait services-stable \
    --cluster "$CLUSTER" \
    --services avatar-backend avatar-frontend avatar-worker

echo ""
echo "✅ Deployed."
echo ""
echo "📊 Summary:"
echo "  Application: $APP_URL"
echo "  ECS cluster: $CLUSTER"
echo "  S3 bucket:   $S3_BUCKET"
echo "  CloudFront:  https://$CLOUDFRONT_DOMAIN   (serves media, not the app)"
echo "  Database:    $DB_ENDPOINT"
echo "  Redis:       $REDIS_ENDPOINT"
echo ""
echo "🔗 Next steps:"
echo "  1. Open $APP_URL"
echo "  2. Put TLS in front of it before real use — the listener is HTTP:80, so"
echo "     AUTH_COOKIE_SECURE=true means the auth cookie will not be sent over it."
echo "     Add an ACM certificate and an HTTPS listener, or front it with CloudFront."
echo "  3. Logs: aws logs tail /ecs/avatar-production/backend --follow"
