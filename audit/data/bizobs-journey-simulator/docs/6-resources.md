--8<-- "snippets/6-resources.js"

# Resources

## 📚 Documentation Links

### Dynatrace Resources
- [Create a Free Trial in Dynatrace](https://www.dynatrace.com/signup/){target="_blank"}
- [Dynatrace Documentation](https://docs.dynatrace.com){target="_blank"}
- [Dynatrace Business Observability](https://docs.dynatrace.com/docs/platform/business-analytics) - Official business analytics documentation
- [OneAgent Installation](https://docs.dynatrace.com/docs/ingest-from/dynatrace-oneagent/installation-and-operation) - OneAgent deployment guide
- [Business Events API](https://docs.dynatrace.com/docs/platform-modules/business-analytics/ba-api-ingest) - Business events ingestion
- [Distributed Tracing](https://docs.dynatrace.com/docs/observe-and-explore/applications-and-microservices/distributed-tracing) - End-to-end trace analysis
- [Dynatrace Blog](https://www.dynatrace.com/news/blog/){target="_blank"}

### Business Observability Concepts  
- [Customer Journey Mapping](https://www.dynatrace.com/news/blog/what-is-customer-journey-mapping/) - Understanding customer experiences
- [Business-Centric Monitoring](https://www.dynatrace.com/news/blog/business-centric-monitoring/) - Monitoring from business perspective
- [Digital Experience Monitoring](https://www.dynatrace.com/platform/digital-experience-monitoring/) - User experience optimization

## 🔧 API Reference

### Journey Simulation API

#### Start Journey Simulation
```http
POST /api/journey/simulate
Content-Type: application/json

{
  "persona": "karen",
  "journey": "insurance-policy",
  "options": {
    "thinkTime": 3000,
    "errorRate": 0.05,
    "businessContext": {
      "campaign": "summer-promo",
      "channel": "web-mobile"
    }
  }
}
```

#### Get Journey Status
```http
GET /api/journey/status/{journeyId}

Response:
{
  "journeyId": "abc123",
  "status": "completed",
  "persona": "karen",
  "duration": 4250,
  "steps": [
    {
      "name": "policy-discovery",
      "duration": 823,
      "status": "success"
    }
  ],
  "businessValue": 1200
}
```

## 🎯 Demo Scripts

### Executive Demo Script (15 minutes)

**Setup (2 minutes)**
1. Open BizObs interface in browser
2. Show Dynatrace environment (Services, Business Events dashboards)
3. Introduce customer personas and journey types

**Demo Flow (10 minutes)**
- Normal Operations: Show successful customer journeys
- Business Impact: Demonstrate how technical issues affect different personas
- Proactive Resolution: Show business metric alerts and technical fixes

## 🤝 Community & Support

### GitHub Repository
- **Main Repository**: [Partner-PowerUp-BizObs-App](https://github.com/lawrobar90/Partner-PowerUp-BizObs-App)
- **Issues & Feature Requests**: [GitHub Issues](https://github.com/lawrobar90/Partner-PowerUp-BizObs-App/issues)

### Dynatrace Community
- **Dynatrace Community**: [community.dynatrace.com](https://community.dynatrace.com)
- **Business Observability Forum**: Discussions on business-centric monitoring

!!! tip "Stay Updated"
    - Star the GitHub repository for updates
    - Join the Dynatrace Community for best practices
    - Follow the documentation for new features and capabilities

<div class="grid cards" markdown>
- [🚀 What's Next? :octicons-arrow-right-24:](7-whats-next.md)
</div>
