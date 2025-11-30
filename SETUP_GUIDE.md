# 🎬 AI Avatar System - Complete Setup Guide

## 📋 Prerequisites

### Required Software
- **Docker** (20.10+) and **Docker Compose** (2.0+)
- **Python** 3.10+
- **Node.js** 18+
- **AWS CLI** configured with credentials
- **Git**
- **GPU** (Optional but recommended for avatar animation)
  - NVIDIA GPU with CUDA 11.8+
  - NVIDIA Container Toolkit installed

### AWS Services Needed
- S3 (Storage)
- RDS PostgreSQL (Database)
- ElastiCache Redis (Caching)
- ECS/Fargate (Container orchestration)
- CloudFront (CDN)
- Route53 (DNS - optional)

### API Keys Required
- **Anthropic API Key** (for Claude)
- **OpenAI API Key** (for Whisper/GPT-4 - optional)
- **AWS Credentials** (Access Key ID and Secret Access Key)

---

## 🚀 Quick Start (Local Development)

### Step 1: Clone and Setup

```bash
cd C:\Users\punit\Downloads\Avatar

# Copy environment file
copy .env.example .env
```

### Step 2: Configure Environment Variables

Edit `.env` file with your credentials:

```env
# AWS Configuration
AWS_ACCESS_KEY_ID=AKIAXXXXXXXXXXXXX
AWS_SECRET_ACCESS_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
AWS_REGION=us-east-1
S3_BUCKET_NAME=your-avatar-bucket-name

# API Keys
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxxxxxxx
OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxx

# Database (auto-configured for Docker)
DATABASE_PASSWORD=SuperSecurePassword123!

# Application
SECRET_KEY=your-super-secret-key-change-this
ENVIRONMENT=development
DEBUG=true
```

### Step 3: Start Services

```bash
# Start all services
docker-compose up -d

# View logs
docker-compose logs -f

# Check service status
docker-compose ps
```

### Step 4: Access Application

- **Frontend**: http://localhost:3000
- **Backend API**: http://localhost:8000
- **API Documentation**: http://localhost:8000/docs
- **Celery Flower**: http://localhost:5555

---

## 📦 Installation Methods

### Method 1: Docker (Recommended)

```bash
# Build and start
docker-compose up --build -d

# Stop
docker-compose down

# Stop and remove volumes
docker-compose down -v
```

### Method 2: Manual Installation

#### Backend Setup

```bash
cd backend

# Create virtual environment
python -m venv venv

# Activate virtual environment
# Windows:
venv\Scripts\activate
# Linux/Mac:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run migrations
alembic upgrade head

# Start server
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

#### Frontend Setup

```bash
cd frontend

# Install dependencies
npm install

# Start development server
npm run dev
```

---

## 🏗️ AWS Deployment

### Step 1: Setup AWS Infrastructure

```bash
# Initialize Terraform
cd infrastructure
terraform init

# Create terraform.tfvars
cat > terraform.tfvars <<EOF
aws_region      = "us-east-1"
environment     = "production"
s3_bucket_name  = "my-avatar-system-storage"
db_password     = "SuperSecurePassword123!"
EOF

# Plan deployment
terraform plan -out=tfplan

# Apply infrastructure
terraform apply tfplan
```

### Step 2: Deploy Application

```bash
# Make deploy script executable
chmod +x deploy.sh

# Deploy to AWS
./deploy.sh production
```

### Step 3: Setup DNS (Optional)

1. Go to Route53 in AWS Console
2. Create a hosted zone for your domain
3. Create A record pointing to CloudFront distribution
4. Update nameservers at your domain registrar

---

## 🔧 Configuration

### Avatar Engine Options

**SadTalker** (Best quality, slower):
```env
AVATAR_ENGINE=sadtalker
AVATAR_RESOLUTION=512
```

**Live Portrait** (Faster, good quality):
```env
AVATAR_ENGINE=liveportrait
AVATAR_RESOLUTION=512
```

**Simple** (Fastest, static + audio):
```env
AVATAR_ENGINE=simple
```

### LLM Configuration

**Use Claude (Recommended)**:
```env
LLM_PROVIDER=anthropic
LLM_MODEL=claude-sonnet-4-20250514
ANTHROPIC_API_KEY=your_key_here
```

**Use GPT-4**:
```env
LLM_PROVIDER=openai
LLM_MODEL=gpt-4-turbo-preview
OPENAI_API_KEY=your_key_here
```

**Use Local Llama 3**:
```env
LLM_PROVIDER=ollama
LLM_MODEL=llama3
# Requires Ollama installed locally
```

### STT/TTS Configuration

**Whisper (STT)**:
```env
STT_PROVIDER=whisper
WHISPER_MODEL=base  # tiny, base, small, medium, large
```

**Coqui TTS**:
```env
TTS_PROVIDER=coqui
```

---

## 📊 Monitoring & Maintenance

### View Logs

```bash
# All services
docker-compose logs -f

# Specific service
docker-compose logs -f backend

# Last 100 lines
docker-compose logs --tail=100 backend
```

### Database Management

```bash
# Access PostgreSQL
docker-compose exec postgres psql -U avatar_user -d avatar_db

# Backup database
docker-compose exec postgres pg_dump -U avatar_user avatar_db > backup.sql

# Restore database
docker-compose exec -T postgres psql -U avatar_user avatar_db < backup.sql
```

### Redis Management

```bash
# Access Redis CLI
docker-compose exec redis redis-cli

# Clear cache
docker-compose exec redis redis-cli FLUSHALL
```

### Celery Tasks

```bash
# View Celery Flower dashboard
Open http://localhost:5555

# Restart worker
docker-compose restart celery-worker
```

---

## 🧪 Testing

### Backend Tests

```bash
cd backend

# Run all tests
pytest

# Run with coverage
pytest --cov=app tests/

# Run specific test
pytest tests/test_avatars.py -v
```

### Frontend Tests

```bash
cd frontend

# Run tests
npm test

# Run with coverage
npm test -- --coverage
```

### Integration Tests

```bash
# Run integration tests
docker-compose -f docker-compose.test.yml up --abort-on-container-exit
```

---

## 🔐 Security Best Practices

1. **Environment Variables**
   - Never commit `.env` file
   - Use AWS Secrets Manager for production
   - Rotate API keys regularly

2. **Database**
   - Use strong passwords
   - Enable SSL connections
   - Regular backups

3. **API**
   - Enable rate limiting
   - Use JWT authentication
   - HTTPS only in production

4. **S3**
   - Enable bucket versioning
   - Use presigned URLs for sensitive content
   - Enable CloudFront for CDN

---

## 🐛 Troubleshooting

### Common Issues

**Problem**: Database connection error
```bash
# Solution: Check PostgreSQL is running
docker-compose ps postgres
docker-compose logs postgres
```

**Problem**: GPU not detected
```bash
# Solution: Install NVIDIA Container Toolkit
# https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html
```

**Problem**: Out of memory
```bash
# Solution: Increase Docker memory limit
# Docker Desktop > Settings > Resources > Memory
```

**Problem**: S3 upload fails
```bash
# Solution: Check AWS credentials
aws s3 ls
aws sts get-caller-identity
```

**Problem**: Avatar generation is slow
```bash
# Solution: 
# 1. Use GPU if available
# 2. Reduce AVATAR_RESOLUTION
# 3. Use "simple" avatar engine for faster results
```

---

## 📈 Performance Optimization

### Backend Optimization

1. **Enable GPU acceleration**
   ```yaml
   # In docker-compose.yml
   deploy:
     resources:
       reservations:
         devices:
           - driver: nvidia
             count: 1
             capabilities: [gpu]
   ```

2. **Increase workers**
   ```bash
   # In Dockerfile CMD
   CMD ["uvicorn", "main:app", "--workers", "4"]
   ```

3. **Enable caching**
   - Use Redis for session data
   - Enable avatar video caching in database

### Frontend Optimization

1. **Enable Next.js image optimization**
2. **Use CDN for static assets**
3. **Enable compression**

### Database Optimization

1. **Add indexes**
   ```sql
   CREATE INDEX idx_avatar_user_id ON avatars(user_id);
   CREATE INDEX idx_session_avatar_id ON sessions(avatar_id);
   ```

2. **Connection pooling**
   ```python
   # In config.py
   pool_size=10
   max_overflow=20
   ```

---

## 📚 API Documentation

### Upload Avatar
```http
POST /api/v1/avatars/upload
Content-Type: multipart/form-data

name: "My Avatar"
file: [image file]
```

### Create Session
```http
POST /api/v1/sessions/create
Content-Type: application/json

{
  "avatar_id": "xxx-xxx-xxx",
  "settings": {}
}
```

### WebSocket Connection
```javascript
const ws = new WebSocket('ws://localhost:8000/ws/session/{session_id}')

// Send text message
ws.send(JSON.stringify({
  type: 'text',
  data: { text: 'Hello!' }
}))

// Send audio
ws.send(JSON.stringify({
  type: 'audio',
  data: { audio: base64AudioData }
}))
```

---

## 🤝 Support

### Get Help

1. Check documentation: https://github.com/yourusername/avatar-system
2. Open an issue: https://github.com/yourusername/avatar-system/issues
3. Email support: support@yourdomain.com

### Contributing

Contributions are welcome! Please read CONTRIBUTING.md for guidelines.

---

## 📄 License

MIT License - See LICENSE file for details

---

**Built with ❤️ for production use**
