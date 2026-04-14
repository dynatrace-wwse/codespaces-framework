--8<-- "snippets/5-troubleshooting.js"

# Troubleshooting

Common issues and solutions for the BizObs Journey Simulator.

## 🚨 Common Issues

### Application Won't Start

#### Issue: Server fails to start
**Symptoms:**
- Error: `EADDRINUSE: address already in use :::8080`
- Server exits immediately after startup

**Solutions:**
```bash
# Check what's using port 8080
lsof -i :8080

# Kill existing Node.js processes
pkill -f "node.*server.js"

# Use alternative port
export PORT=8081
npm start
```

#### Issue: Missing dependencies
**Symptoms:**
- Module not found errors
- NPM installation failures

**Solutions:**
```bash
# Clear NPM cache
npm cache clean --force

# Remove node_modules and reinstall
rm -rf node_modules package-lock.json
npm install

# Use specific Node.js version
nvm use 18
npm install
```

### Dynatrace Integration Issues

#### Issue: No traces appear in Dynatrace
**Symptoms:**
- Journey simulations run successfully
- No distributed traces visible in Dynatrace
- No business events captured

**Diagnosis Steps:**
```bash
# Check OneAgent status
sudo /opt/dynatrace/oneagent/agent/tools/oactl status

# Verify environment variables
echo $DYNATRACE_URL
echo $DYNATRACE_TOKEN

# Test API connectivity
curl -H "Authorization: Api-Token $DYNATRACE_TOKEN" \
     "$DYNATRACE_URL/api/v1/config/clusterversion"
```

**Solutions:**

1. **OneAgent Installation:**
```bash
# Download and install OneAgent
wget -O oneagent.sh "https://YOUR-ENVIRONMENT.live.dynatrace.com/api/v1/deployment/installer/agent/unix/default/latest?arch=x86&flavor=default"
sudo sh oneagent.sh --set-app-log-content-access=true
```

2. **API Token Permissions:**
   Required scopes:
   - `logs.ingest`
   - `metrics.ingest`
   - `events.ingest`
   - `bizevents.ingest`

3. **Environment Variables (Codespaces):**
```bash
# In Codespaces settings, add secrets:
DYNATRACE_URL=https://your-environment.live.dynatrace.com
DYNATRACE_TOKEN=dt0c01.ABC123...
```

#### Issue: Business events not appearing
**Symptoms:**
- Technical traces work
- No business events in Dynatrace

**Solutions:**
```bash
# Check business events configuration
curl -X POST "$DYNATRACE_URL/api/v2/bizevents/ingest" \
  -H "Authorization: Api-Token $DYNATRACE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"eventType": "test", "test": true}'

# Enable business events in OneAgent
sudo /opt/dynatrace/oneagent/agent/tools/oactl --set-business-events=true
```

### Journey Simulation Problems

#### Issue: Journeys fail at specific steps
**Symptoms:**
- Consistent failures at same journey step
- Specific persona always fails
- Timeout errors in logs

**Diagnosis:**
```bash
# Check service health
curl http://localhost:8080/api/health

# Check specific service endpoints  
curl http://localhost:8081/health  # Quote service
curl http://localhost:8082/health  # Payment service
curl http://localhost:8083/health  # Policy service

# View detailed logs
tail -f logs/application.log
tail -f logs/journey-simulation.log
```

**Solutions:**

1. **Service Dependencies:**
```bash
# Restart all services
./restart.sh

# Check port availability
netstat -tlnp | grep 808[0-9]

# Verify database connections
curl http://localhost:8080/api/database/status
```

2. **Memory Issues:**
```bash
# Check memory usage
free -h
ps aux | grep node | head -10

# Restart with memory limits
node --max-old-space-size=4096 server.js
```

### Performance Issues

#### Issue: Slow journey execution
**Symptoms:**
- Journeys take >30 seconds to complete
- High CPU usage
- Memory warnings

**Diagnosis:**
```bash
# Monitor system resources
top -p $(pgrep -f "node.*server.js")

# Check Node.js memory
curl http://localhost:8080/api/system/memory

# Profile journey performance
curl http://localhost:8080/api/journey/profile/karen/insurance-policy
```

**Solutions:**

1. **Optimize Think Times:**
```json
{
  "persona": "karen",
  "thinkTime": 1000,  // Reduce from 3000ms for testing
  "concurrentJourneys": 5  // Reduce concurrent load
}
```

2. **Database Optimization:**
```bash
# Enable connection pooling
export DB_POOL_SIZE=10

# Use in-memory mode for testing
export DB_MODE=memory
```

## 🔧 Configuration Issues

### Environment Variables

#### Missing Configuration
**Required Variables:**
```bash
# Essential settings
PORT=8080
NODE_ENV=development

# Dynatrace integration (optional)
DYNATRACE_URL=https://your-tenant.live.dynatrace.com
DYNATRACE_TOKEN=dt0c01.YOUR_TOKEN

# Advanced features (optional)
LOADRUNNER_ENABLED=false
ERROR_SIMULATION_ENABLED=true
BUSINESS_EVENTS_ENABLED=true
```

#### Codespaces-Specific Configuration
```bash
# Set in Codespaces secrets
gh secret set DYNATRACE_URL --body "https://your-tenant.live.dynatrace.com"
gh secret set DYNATRACE_TOKEN --body "dt0c01.YOUR_TOKEN"

# Verify in running Codespace
env | grep DYNATRACE
```

### Network Configuration

#### Issue: Services can't communicate
**Symptoms:**
- Connection refused errors
- DNS resolution failures
- Intermittent connectivity

**Solutions:**
```bash
# Check service discovery
curl http://localhost:8080/api/services/status

# Verify port bindings
sudo netstat -tlnp | grep :808

# Test cross-service communication
curl http://localhost:8081/api/test-connectivity
```

## 🐛 Debugging Guide

### Enable Debug Logging

#### Application Debug Mode
```bash
# Start with debug logging
DEBUG=bizobs:* npm start

# Or set environment variable
export DEBUG=bizobs:journey,bizobs:personas
npm start
```

#### Journey Trace Debugging
```bash
# Enable journey tracing
export JOURNEY_DEBUG=true
export TRACE_ALL_STEPS=true

# View real-time journey logs
tail -f logs/journey-debug.log
```

### Diagnostic Endpoints

#### Health Check Endpoints
```bash
# Overall health
curl http://localhost:8080/api/health

# Detailed system status
curl http://localhost:8080/api/status | jq

# Service-specific health
curl http://localhost:8080/api/services/health
```

#### Debug Information
```bash
# Current configuration
curl http://localhost:8080/api/debug/config

# Active journeys
curl http://localhost:8080/api/debug/journeys

# Memory and performance
curl http://localhost:8080/api/debug/system
```

### Common Error Messages

#### "Journey simulation failed"
**Cause:** Service unavailable or configuration error
**Solution:**
```bash
# Check service status
curl http://localhost:8080/api/services/status

# Restart services
./restart.sh

# Check configuration
curl http://localhost:8080/api/debug/config
```

#### "Persona not found"
**Cause:** Invalid persona name in journey request
**Solution:**
```bash
# List available personas
curl http://localhost:8080/api/personas

# Verify persona configuration
curl http://localhost:8080/api/personas/karen
```

#### "Business event ingestion failed"
**Cause:** Dynatrace API token or connectivity issue
**Solution:**
```bash
# Test API connectivity
curl -H "Authorization: Api-Token $DYNATRACE_TOKEN" \
     "$DYNATRACE_URL/api/v1/config/clusterversion"

# Check token permissions
curl -H "Authorization: Api-Token $DYNATRACE_TOKEN" \
     "$DYNATRACE_URL/api/v2/bizevents/ingest" \
     -X POST -d '{"eventType":"test","test":true}'
```

## 📊 Monitoring & Alerts

### Application Monitoring

#### Key Metrics to Watch
```bash
# Journey success rate (should be >95%)
curl http://localhost:8080/api/metrics/journey-success-rate

# Average journey duration (should be <10 seconds)  
curl http://localhost:8080/api/metrics/journey-duration

# Service availability (should be 100%)
curl http://localhost:8080/api/metrics/service-availability

# Memory usage (should be <80%)
curl http://localhost:8080/api/metrics/memory-usage
```

#### Setting Up Alerts
```json
{
  "alerts": [
    {
      "metric": "journey-success-rate",
      "threshold": 0.95,
      "operator": "less-than",
      "action": "restart-services"
    },
    {
      "metric": "average-journey-duration", 
      "threshold": 15000,
      "operator": "greater-than",
      "action": "check-performance"
    }
  ]
}
```

### Log Analysis

#### Important Log Locations
```bash
# Application logs
tail -f logs/application.log

# Journey simulation logs
tail -f logs/journey-simulation.log

# Error logs
tail -f logs/error.log

# Dynatrace integration logs
tail -f logs/dynatrace.log
```

#### Log Analysis Commands
```bash
# Find recent errors
grep "ERROR" logs/application.log | tail -10

# Count journey failures by type
grep "Journey failed" logs/journey-simulation.log | cut -d':' -f2 | sort | uniq -c

# Monitor real-time activity
tail -f logs/application.log | grep -E "(Journey|ERROR|WARNING)"
```

## 🆘 Getting Help

### Self-Service Resources

1. **Check Application Status:**
```bash
curl http://localhost:8080/api/status | jq .
```

2. **Review Recent Logs:**
```bash
tail -100 logs/application.log
```

3. **Verify Configuration:**
```bash
curl http://localhost:8080/api/debug/config
```

4. **Test Basic Functionality:**
```bash
curl -X POST http://localhost:8080/api/journey/simulate \
  -H "Content-Type: application/json" \
  -d '{"persona":"karen","journey":"insurance-policy"}'
```

### Escalation Path

If issues persist after trying the solutions above:

1. **Collect Diagnostic Information:**
```bash
# Generate diagnostic report
curl http://localhost:8080/api/debug/report > diagnostic-report.json

# Gather system information  
uname -a > system-info.txt
df -h >> system-info.txt
free -h >> system-info.txt
```

2. **Contact Information:**
   - GitHub Issues: [Create an issue](https://github.com/lawrobar90/Partner-PowerUp-BizObs-App/issues)
   - Documentation: [View latest docs](https://dynatrace-wwse.github.io/bizobs-journey-simulator/)

!!! tip "Quick Resolution Tips"
    - Most issues resolve with a simple restart: `./restart.sh`
    - Check logs first: `tail -f logs/application.log`  
    - Verify Dynatrace connectivity: Test API token and URL
    - Use debug mode: `DEBUG=bizobs:* npm start`

<div class="grid cards" markdown>
- [📚 Resources & References :octicons-arrow-right-24:](6-resources.md)
</div>
