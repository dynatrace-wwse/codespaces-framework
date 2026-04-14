--8<-- "snippets/codespaces.js"

## Launch GitHub Codespaces

GitHub Codespaces provides the fastest way to get started with the BizObs Journey Simulator. The environment comes pre-configured with all dependencies, extensions, and integrations ready to use.

!!! tip "Recommended Approach"
    Codespaces eliminates setup complexity and provides a consistent, cloud-based development environment perfect for demos and learning.

## 🚀 Quick Launch Steps

### 1. Access the Repository
Navigate to the BizObs Journey Simulator repository:
```
https://github.com/dynatrace-wwse/bizobs-journey-simulator
```

### 2. Create Codespace
1. **Click** the green "**Code**" button
2. **Select** the "**Codespaces**" tab  
3. **Click** "**Create codespace on main**"
4. **Wait** for the environment to initialize (2-3 minutes)

![Codespaces Launch](img/codespaces_installing.png)

### 3. Automatic Configuration
The Codespace automatically:
- ✅ **Installs Node.js 18+** runtime environment
- ✅ **Installs npm dependencies** for the BizObs application
- ✅ **Configures port forwarding** for web interface (8080) and services (8081-8120)
- ✅ **Sets up VS Code extensions** for optimal development experience
- ✅ **Initializes the application** with core services running

![Codespaces Complete](img/codespaces_finish.png)

## 🔧 What's Pre-Configured

### Development Environment
- **Node.js Runtime**: Version 18+ with npm package manager
- **VS Code Extensions**: Debugging, JSON formatting, HTTP client tools
- **Terminal Access**: Full bash terminal for command execution
- **File System**: Complete repository access with editing capabilities

### Application Stack
- **Main Application**: Business Observability Generator on port 8080
- **Core Services**: Three essential services (Discovery, Purchase, DataPersistence)
- **Dynamic Port Management**: Automatic port allocation for generated services
- **Service Health Monitoring**: Built-in health checks and status reporting

### Port Configuration
The Codespace automatically forwards these ports:
```bash
Port 8080  -> Main Web Interface (Public)
Port 8081+ -> Dynamic Microservices (Private)
```

## 🌐 Accessing Your Application

### Web Interface
Once the Codespace is ready:

1. **Look for the notification**: "Your application running on port 8080 is available"
2. **Click** "**Open in Browser**" or navigate to the forwarded URL
3. **Verify** you see the Business Observability Generator welcome page

**Alternative Access:**
- Use the **Ports** tab in VS Code
- Click the **globe icon** next to port 8080
- **Copy** the public URL for sharing

### API Endpoints
Test the API endpoints directly:
```bash
# In the Codespace terminal:
curl http://localhost:8080/api/health
curl http://localhost:8080/api/journey-simulation/health
```

## 🔍 Verify Installation

### 1. Application Health Check
```bash
# Run in the Codespace terminal
curl -s http://localhost:8080/api/health | jq .
```

**Expected Response:**
```json
{
  "status": "ok",
  "timestamp": "2025-11-28T...",
  "mainProcess": {
    "pid": 1234,
    "uptime": 45.2,
    "port": 8080
  },
  "childServices": [
    {
      "service": "DiscoveryService-DefaultCompany",
      "running": true,
      "pid": 1235
    }
  ]
}
```

### 2. Core Services Status
```bash
# Check running Node.js processes
ps aux | grep -E "(Service|Process)" | grep -v grep
```

**Expected Output:** Should show 3+ Node.js processes running

### 3. Port Allocation
```bash
# Check port usage
netstat -tulpn | grep :80
```

**Expected Output:** Ports 8080-8083+ should be in use

## 🎯 Quick Test Journey

### Create Your First Journey
Use the web interface or API to create a test journey:

**Via Web Interface:**
1. **Navigate** to the forwarded port 8080 URL
2. **Click** "Get Started"
3. **Fill out** the customer details form:
   - Company Name: `Codespace Test Corp`
   - Domain: `test.codespace.com`
   - Industry Type: `Technology`
   - Journey Type: `Environment Validation`

**Via API (in terminal):**
```bash
curl -X POST http://localhost:8080/api/journey-simulation/simulate-journey \
  -H "Content-Type: application/json" \
  -d '{
    "companyName": "CodespaceTestCorp",
    "domain": "test.codespace.com",
    "industryType": "Technology",
    "journey": {
      "companyName": "CodespaceTestCorp",
      "domain": "test.codespace.com",
      "industryType": "Technology", 
      "journeyType": "Environment Validation",
      "journeyDetail": "Codespace Setup Test",
      "steps": [
        {"stepName": "Setup", "description": "Environment setup verification"},
        {"stepName": "Validation", "description": "System validation check"}
      ]
    },
    "journeyId": "codespace_test_001",
    "customerId": "test_001"
  }' | jq .
```

**Expected Result:**
- New services created and running
- Complete journey response with business metadata
- Services visible in process list

## 🛠️ Troubleshooting

### Common Issues

**Application Not Starting:**
```bash
# Check logs
cd "/workspaces/bizobs-journey-simulator/BizObs Generator"
npm start
```

**Port 8080 Already in Use:**
```bash
# Kill any existing processes
pkill -f "node server.js"
npm start
```

**Services Not Creating:**
```bash
# Check service manager logs
tail -f logs/bizobs.log
```

**Dependencies Missing:**
```bash
# Reinstall dependencies
cd "/workspaces/bizobs-journey-simulator/BizObs Generator"
npm install
npm start
```

### Advanced Configuration

**Custom Environment Variables:**
```bash
# Set custom configuration (optional)
export MAIN_SERVER_PORT=8080
export SERVICE_PORT_RANGE_START=8081
export SERVICE_PORT_RANGE_END=8120
```

**Development Mode:**
```bash
# Run with debug logging
DEBUG=* npm start
```

## 🔗 Dynatrace Integration

### Connect Your Environment
To see the business observability data in Dynatrace:

1. **Ensure** your Dynatrace environment is configured (see Getting Started guide)
2. **Optional**: Configure OneAgent if you want to see services in Dynatrace
3. **Run** journey simulations to generate business events
4. **Verify** data appears in Dynatrace BizEvents and Services views

### Public URL Sharing
The Codespace provides a public URL for sharing:
- **Copy** the forwarded port 8080 URL from the Ports tab
- **Share** with colleagues for collaborative demos
- **Use** in Dynatrace synthetic monitoring (if desired)

!!! success "Codespace Ready!"
    Your BizObs Journey Simulator is now running in a fully configured cloud environment. You're ready to start creating comprehensive customer journeys with business observability data.

<div class="grid cards" markdown>
- [🛤️ Create Your First Journey :octicons-arrow-right-24:](journey1-azure-enterprise.md)
</div>
