# Deployment Guide

## ✅ Production Ready Checklist

Your backend-python is ready for deployment with these files:
- ✅ `simplified_bus_server.py` - Main server
- ✅ `requirements.txt` - Dependencies (without heavy face-recognition)
- ✅ `Procfile` - For Heroku/Railway
- ✅ `railway.json` - Railway configuration
- ✅ `runtime.txt` - Python 3.11.9
- ✅ `.env.example` - Environment variables template

## Deploy to Railway (Recommended)

### Step 1: Push to GitHub
```bash
git add .
git commit -m "Production ready deployment"
git push
```

### Step 2: Deploy on Railway
1. Go to https://railway.app
2. Sign in with GitHub
3. Click "New Project" → "Deploy from GitHub repo"
4. Select your `backend-python` repository
5. Railway will automatically detect Python and deploy

### Step 3: Environment Variables (Optional)
Add these in Railway dashboard under "Variables":
- `MONGODB_URI` - Your MongoDB connection string (if you want to override hardcoded one)
- `PORT` - Railway sets this automatically

### Step 4: Generate Domain
1. Go to "Settings" tab
2. Click "Generate Domain"
3. Get your URL: `https://backend-python-production-xxxx.up.railway.app`

## Common Deployment Errors & Fixes

### Error: "dlib installation failed"
**Fixed!** We removed dlib from requirements.txt since face recognition is handled by ESP32 devices.

### Error: "Port already in use"
**Fixed!** Server uses `PORT` environment variable from Railway.

### Error: "Module not found"
Make sure all files are pushed to GitHub:
- `simplified_bus_server.py`
- `route_detector.py`
- `utils/dynamic_schedule_manager.py`

## Test Your Deployment

Once deployed, test these endpoints:

```bash
# Replace YOUR_URL with your Railway URL

# Check server status
curl https://YOUR_URL/status

# Check passengers
curl https://YOUR_URL/passengers

# Check trip status
curl https://YOUR_URL/trip

# Check schedule
curl https://YOUR_URL/api/schedule
```

## Local Testing Before Deploy

```bash
# Install dependencies
pip install -r requirements.txt

# Run server
python simplified_bus_server.py

# Test locally
curl http://localhost:8888/status
```

## Monitoring

Railway provides:
- Real-time logs
- Metrics (CPU, Memory, Network)
- Automatic restarts on failure

Access logs in Railway dashboard → Your Project → "Deployments" tab

## Need Help?

If deployment fails:
1. Check Railway logs for error messages
2. Verify all files are in GitHub
3. Make sure MongoDB connection string is correct
4. Check that PORT environment variable is set by Railway
