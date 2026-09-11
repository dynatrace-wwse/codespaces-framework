

!!! example ""
    Monitoring is a critical aspect of maintaining quality, reliability, and visibility across all repositories and their instantiations in the enablement framework. By tracking usage and interactions, we ensure every codespace and its associated resources operate as expected and deliver value.
 

## 📡 Why Monitoring Matters

- **Visibility:** Gain insights into how repositories and codespaces are used and adopted.
- **Quality Assurance:** Detect issues early and ensure all deployments meet expected standards.
- **Business Insights:** Understand usage patterns and derive business value from operational data.
- **Showcase Best Practices:** Demonstrate the power of Dynatrace by monitoring our own codespaces (“Drink your own Champagne” principle).


!!! example ""
    ![monitoring](img/monitoring.png){ align=center ; width="800";}


## Monitoring Approach

- **GitHub Pages & Agentless RUM:**  
  Every codespace is associated with a GitHub Page. For each GitHub Page, an agentless Real User Monitoring (RUM) application is created, enabling end-to-end visibility into user interactions and performance.

- **Automated Tracking on Creation:**  
  When a new codespace is instantiated, the `finalizePostCreation` function sends a JSON payload to  
  [codespaces-tracker.whydevslovedynatrace.com/api/receive](https://codespaces-tracker.whydevslovedynatrace.com/api/receive).  
  - Only requests with the correct authentication header are accepted.
  - This payload contains metadata about the codespace, its repository, and its usage context.

- **Codespaces-Tracker Service:**  
  The Codespaces-Tracker is a Spring Boot application deployed on a GKE (Google Kubernetes Engine) cluster with three replicas for high availability.
  - It processes incoming payloads, enriches them with geo-information, and logs the JSON payload.
  - The monitoring OneAgent generates BizEvents from the pod logs, enabling advanced business-related log use cases and analytics.

## Benefits

- **Comprehensive Monitoring:**  
  All codespaces and their GitHub Pages are monitored for activity, performance, and adoption.
- **Enhanced Observability:**  
  Real-time data collection and enrichment provide actionable insights for both technical and business stakeholders.
- **Demonstration of Dynatrace Capabilities:**  
  By monitoring our own codespaces, we showcase Dynatrace’s observability features in real-world scenarios.

---


## Implementation

### 🌎 Codespaces Instantiations

All codespace instantiations are monitored by sending a signal to the Codespaces-Tracker service running in the GKE cluster. This is achieved through the `verifyContainerCreation` function, which validates the successful creation of a codespace. 

- **verifyContainerCreation:**  
  Checks that the codespace environment has been set up correctly and all required components are running. Once verification is complete, it calls the `postCodespaceTracker` function.
- **postCodespaceTracker:**  
  Sends a JSON payload containing metadata about the codespace (such as repository name, user, and environment details) to the Codespaces-Tracker API endpoint. The payload is authenticated and enriched with geo-information, and BizEvents are generated from the logs for further analysis.

### 📊 GitHub Pages

GitHub Pages are monitored using Dynatrace Agentless Real User Monitoring (RUM). Both mechanisms live
in **one file** — `docs/overrides/main.html`, the Jinja template that extends the Material theme's
`base.html`. No markdown page carries tracking code of its own:

- The agentless RUM snippet is injected into the template's `libs` block, so every page load is
  tracked for user interactions and performance.
- The same template's `scripts` block sends a `page_load` BizEvent carrying the page title, which is
  what allows navigation and engagement to be analysed per page.

Both blocks are guarded on `config.extra.rum_snippet`. That key is the **only** monitoring
configuration a consuming repo provides — it lives in the repo's own `mkdocs.yaml` under `extra:`. If
it is absent, the template renders nothing and the site is simply unmonitored, which is why
`sync validate` checks for it.

!!! note "`main.html` is committed per repo"
    Unlike `mkdocs-base.yaml` and `extra.css`, which the docs workflow fetches from the framework at
    build time, `docs/overrides/main.html` is a committed copy in each repository. A change to the
    tracking mechanism therefore has to be rolled out with `sync push-update`, not picked up
    automatically on the next build.

---


## Learn More

- [Dynatrace Agentless RUM Documentation](https://www.dynatrace.com/support/help/shortlink/agentless-rum)
- [Business Events with Dynatrace](https://www.dynatrace.com/support/help/shortlink/bizevents)


<div class="grid cards" markdown>
- [Continue to Versioning →](versioning.md)
</div>