# App Functionality Showcase

This section demonstrates the **actual capabilities** of the BizObs Journey Simulator with real-world test use cases, API endpoints, and practical examples. All examples below are based on the live application functionality.

## 🎯 Core Application Features

### 1. **Dynamic Service Architecture**
The app automatically spawns and manages microservices as child processes with proper Dynatrace service splitting:

```javascript
// Real service management from server.js
process.env.DT_SERVICE_NAME = 'bizobs-main-server';
process.env.DT_LOGICAL_SERVICE_NAME = 'bizobs-main-server';
process.env.DT_TAGS = 'service=bizobs-main-server';
process.env.DT_CUSTOM_PROP = 'service.splitting=enabled';
```

**Available Services:**
- `DiscoveryService` - Product/service discovery functionality
- `PurchaseService` - Transaction and payment processing
- `DataPersistenceService` - Data storage and retrieval
- Dynamic services spawned per journey step as needed

### 2. **LoadRunner Integration** 
Generates real LoadRunner C-scripts with comprehensive test scenarios:

**Load Test Configurations:**
```javascript
const LOADRUNNER_CONFIGS = {
  light: { journeyInterval: 30, duration: 600 },    // 20 users over 10 min
  medium: { journeyInterval: 15, duration: 900 },   // 60 users over 15 min  
  heavy: { journeyInterval: 10, duration: 1200 },   // 120 users over 20 min
  stress: { journeyInterval: 5, duration: 1800 },   // 360 users over 30 min
  extreme: { journeyInterval: 3, duration: 1800 },  // 600 users over 30 min
  peak: { journeyInterval: 2, duration: 1200 }      // 600 users over 20 min
};
```

### 3. **Customer Error Profiles**
Realistic error simulation based on customer characteristics:

```javascript
const CUSTOMER_ERROR_PROFILES = {
  'acme corp': {
    errorRate: 0.15,
    errorTypes: ['payment_gateway_timeout', 'inventory_service_down'],
    httpErrors: [500, 503, 429],
    problematicSteps: ['checkout', 'payment', 'order confirmation']
  },
  'umbrella corporation': {
    errorRate: 0.35,
    errorTypes: ['security_breach_detected', 'system_contamination'],
    httpErrors: [500, 503, 502],
    problematicSteps: ['verification', 'security', 'data processing']
  }
};
```

## 🔧 Real API Endpoints & Examples

### Journey Management APIs

#### **Start a Journey Simulation**
```bash
POST /api/journey-simulation/simulate
Content-Type: application/json

{
  "companyName": "Acme Corp",
  "domain": "acme.com",
  "industryType": "retail",
  "steps": ["Discovery", "Consideration", "Purchase", "Confirmation"],
  "additionalFields": {
    "customerSegment": "premium",
    "region": "north-america"
  }
}
```

**Response:**
```json
{
  "success": true,
  "sessionId": "sess_acme_123456",
  "journeySteps": [
    {
      "stepName": "Discovery",
      "serviceName": "DiscoveryService-AcmeCorp",
      "port": 8083,
      "status": "completed",
      "responseTime": 245,
      "businessMetrics": {
        "revenue": 0,
        "conversionRate": 1.0
      }
    }
  ],
  "totalDuration": 1247,
  "businessImpact": {
    "totalRevenue": 89.50,
    "customerSatisfaction": 4.2
  }
}
```

#### **Batch Journey Simulation**
```bash
POST /api/journey-simulation/simulate-batch-chained
Content-Type: application/json

{
  "customers": [
    {"companyName": "Acme Corp", "domain": "acme.com"},
    {"companyName": "GlobalTech", "domain": "globaltech.io"}
  ],
  "journey": {
    "steps": ["Discovery", "Purchase", "Retention"]
  },
  "thinkTimeMs": 2000
}
```

### LoadRunner Integration APIs

#### **Generate LoadRunner Scripts**
```bash
POST /api/loadrunner/generate
Content-Type: application/json

{
  "journeyConfig": {
    "companyName": "TechStart Inc",
    "domain": "techstart.com", 
    "steps": ["Discovery", "Trial", "Purchase", "Activation"]
  },
  "testConfig": "medium",
  "errorSimulationEnabled": true
}
```

**Generated Output:**
- `loadrunner-tests/TechStart_2024-11-28/TechStart_Journey.c`
- `loadrunner-tests/scenarios/medium-load.json`
- Complete C-script with Dynatrace tagging

#### **List Available Test Scenarios**
```bash
GET /api/loadrunner/scenarios
```

**Response:**
```json
{
  "scenarios": [
    {
      "name": "light",
      "description": "Light load - 20 users over 10 minutes",
      "journeyInterval": 30,
      "duration": 600,
      "estimatedUsers": 20
    },
    {
      "name": "stress", 
      "description": "Stress test - 360 users over 30 minutes",
      "journeyInterval": 5,
      "duration": 1800,
      "estimatedUsers": 360
    }
  ]
}
```

### Service Management APIs

#### **Check Service Health**
```bash
GET /api/services/health
```

**Response:**
```json
{
  "mainServer": {
    "status": "healthy",
    "port": 8080,
    "uptime": 3600
  },
  "childServices": {
    "DiscoveryService-DefaultCompany": {
      "status": "running",
      "port": 8083,
      "pid": 12345,
      "requests": 47
    },
    "PurchaseService-DefaultCompany": {
      "status": "running", 
      "port": 8084,
      "pid": 12346,
      "requests": 23
    }
  }
}
```

#### **Reset All Services**
```bash
POST /api/admin/reset-ports
```

Stops all dynamic services and frees up ports for fresh start.

### Error Simulation APIs

#### **Trigger Custom Errors**
```bash
POST /api/simulate-error
Content-Type: application/json

{
  "errorType": "payment_gateway_timeout",
  "customerName": "Acme Corp", 
  "stepName": "checkout",
  "severity": "high"
}
```

### Metrics & Analytics APIs

#### **Get Journey Metrics**
```bash
GET /api/metrics/journey-summary
```

**Response:**
```json
{
  "totalJourneys": 127,
  "successfulJourneys": 89,
  "conversionRate": 0.70,
  "averageRevenue": 156.78,
  "topPerformingSteps": ["Discovery", "Purchase"],
  "problemAreas": ["Payment", "Verification"],
  "businessMetrics": {
    "totalRevenue": 13952.42,
    "customerSatisfactionScore": 4.1
  }
}
```

## 🧪 Practical Test Use Cases

### **Test Case 1: E-Commerce Journey**
**Scenario:** Simulate a retail customer purchasing process with realistic error patterns.

```bash
# 1. Start the journey
curl -X POST http://localhost:8080/api/journey-simulation/simulate \
  -H "Content-Type: application/json" \
  -d '{
    "companyName": "Fashion Forward",
    "domain": "fashionforward.com",
    "industryType": "retail",
    "steps": ["Discovery", "Browse", "AddToCart", "Checkout", "Payment", "Confirmation"],
    "additionalFields": {
      "customerType": "returning",
      "cartValue": 89.99
    }
  }'

# 2. Monitor progress via WebSocket
# Connect to: ws://localhost:8080/ws/journey-updates

# 3. Check business metrics
curl http://localhost:8080/api/metrics/journey-summary
```

**Expected Results:**
- 6 microservices spawned dynamically
- Real-time business metrics tracking
- Dynatrace service splitting with proper tagging
- LoadRunner script generation capability

### **Test Case 2: Insurance Application Flow**
**Scenario:** Complex multi-step insurance application with validation and underwriting.

```bash
# Insurance journey with higher complexity
curl -X POST http://localhost:8080/api/journey-simulation/simulate \
  -H "Content-Type: application/json" \
  -d '{
    "companyName": "SecureLife Insurance", 
    "domain": "securelife.com",
    "industryType": "financial",
    "steps": ["Quote", "Application", "Verification", "Underwriting", "Approval", "Policy"],
    "additionalFields": {
      "policyType": "life",
      "coverageAmount": 250000,
      "riskLevel": "standard"
    }
  }'
```

### **Test Case 3: Load Testing Generation**
**Scenario:** Generate performance tests for high-traffic scenarios.

```bash
# 1. Generate stress test scenario
curl -X POST http://localhost:8080/api/loadrunner/generate \
  -H "Content-Type: application/json" \
  -d '{
    "journeyConfig": {
      "companyName": "HighTraffic Corp",
      "domain": "hightraffic.com",
      "steps": ["Discovery", "Purchase", "Support"]
    },
    "testConfig": "stress",
    "errorSimulationEnabled": true
  }'

# 2. Check generated files
# loadrunner-tests/HighTraffic_2024-11-28/HighTraffic_Journey.c
# loadrunner-tests/scenarios/stress-test.json
```

### **Test Case 4: Error Resilience Testing**
**Scenario:** Test application behavior under various error conditions.

```bash
# 1. Start normal journey
curl -X POST http://localhost:8080/api/journey-simulation/simulate \
  -H "Content-Type: application/json" \
  -d '{
    "companyName": "Umbrella Corporation",
    "domain": "umbrella.com", 
    "steps": ["Discovery", "Security", "Processing", "Confirmation"]
  }'

# 2. Inject errors mid-journey
curl -X POST http://localhost:8080/api/simulate-error \
  -H "Content-Type: application/json" \
  -d '{
    "errorType": "security_breach_detected",
    "customerName": "Umbrella Corporation",
    "stepName": "Security",
    "severity": "critical"
  }'

# 3. Observe error propagation and recovery
```

## 🔍 Observability Features

### **Dynatrace Integration**
- **Service Splitting**: Each journey step runs as separate service
- **Request Tagging**: Custom tags for business context  
- **Distributed Tracing**: End-to-end journey visibility
- **Business Events**: Revenue and conversion tracking

### **Real-Time Monitoring**
- **WebSocket Updates**: Live journey progress
- **Health Checks**: Service status monitoring
- **Performance Metrics**: Response times and throughput
- **Error Tracking**: Failure patterns and impact analysis

### **Business Analytics** 
- **Revenue Tracking**: Per-journey monetary value
- **Conversion Rates**: Step-by-step success ratios
- **Customer Satisfaction**: Experience quality scoring
- **Performance Impact**: Technical metrics vs business outcomes

## 🚀 Getting Started

1. **Start the Application:**
   ```bash
   cd app
   npm start
   ```

2. **Access the Web Interface:**
   - Open http://localhost:8080
   - Use the interactive dashboard for journey creation

3. **Test API Endpoints:**
   - Use the examples above to test functionality
   - Monitor Dynatrace for service splitting and tracing

4. **Generate Load Tests:**
   - Create LoadRunner scripts from journey simulations
   - Execute performance testing scenarios

The application is now ready for comprehensive business observability testing with real customer journeys, error simulation, and performance analysis!

## 📊 Next: Customer-Specific Test Cases

*Ready to receive your specific customer test use case prompts to create targeted journey simulations and documentation.*
