

!!! tip "Maximizing User Experience with ease of use, best practices, readable code, DevOps tooling, and more..."
	![greeting](img/greeting.png){ align=center ; width="800";}

To maximize adoption and uphold the Dynatrace public image—especially among DevOps stakeholders—repositories using the enablement framework should follow these best practices:

- Present all materials and interfaces professionally and consistently.
- Govern the project with a clear, recognized open source license.
- Tag stable releases to provide clear versioning and facilitate reliable adoption.
- Maintain comprehensive, up-to-date changelogs to document project evolution.
- Structure code for readability and scalability, supporting maintainability and future growth.
- Provide clean, well-organized, and accessible documentation for all users.
- Respond promptly and constructively to issues and community feedback.
- Use inclusive language throughout documentation and communications.
- Display relevant badges (build status, license, coverage) to communicate project health and transparency.
- Incorporate recognizable logos to reinforce project identity.
- Offer interactive, hands-on training resources to accelerate onboarding and skill development.
- Recommend tools such as p10k, k9s, and zsh to further enhance the DevOps experience.

By following these guidelines, the framework remains accessible, professional, and appealing to the broader DevOps and open source communities.



## Tips and Tricks

### 🧈 Navigating the Kubernetes Cluster with k9s

One of the most efficient ways to interact with your Kubernetes cluster is by using `k9s`, a terminal-based UI that streamlines cluster management and troubleshooting.

!!! tip "K9s in action"
	![k9s](img/k9s.png){ align=center ; width="800";}

**Getting Started with k9s:**

- Launch `k9s` by running `k9s` in your terminal (pre-installed in the Codespaces Enablement Framework).
- The interface provides a real-time, interactive view of all your Kubernetes resources—pods, deployments, services, and more.
- Use the arrow keys to navigate, `/` to filter resources, and `:` to enter commands (e.g., `:ctx` to switch contexts, `:ns` to change namespaces, `:pod` to list pods, `:svc` to list services, `:dynakube` to list dynakubes).
- Press `d` to describe a resource, `l` to view logs, and `s` to open a shell into a pod.
- Press `0` (zero) to list all resources, namespaces, and services.
- All actions are keyboard-driven, making it fast and intuitive for both power users and beginners.

**Why use k9s?**

- Rapidly diagnose issues, monitor workloads, and manage resources without leaving your terminal.
- Visualize pod health, events, and logs in real time.
- Reduce context switching and streamline your DevOps workflow.

For more details, see the [k9s documentation](https://k9scli.io/) or try it out in your Codespace now.


<div class="grid cards" markdown>
- [Continue to Synchronizer →](synchronizer.md)
</div>