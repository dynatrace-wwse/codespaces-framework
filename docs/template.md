# Templates

The framework ships two official templates. Choose the one that matches your delivery mode.

| Template | Delivery mode | Best for |
|---|---|---|
| [Codespaces Template](#codespaces-template) | GitHub Codespaces | Labs, workshops, self-paced content delivered via browser Codespace |
| [App Training Template](#app-training-template) | Orbital + Dynatrace app | Scored, interactive trainings rendered inside the Dynatrace product |

---

## Codespaces Template {#codespaces-template}

!!! abstract "The [Enablement Codespaces Template](https://github.com/dynatrace-wwse/enablement-codespaces-template)"
    ![professors](img/dt_professors.png){ align=right ; width="150"}
    The [Enablement Codespaces Template](https://github.com/dynatrace-wwse/enablement-codespaces-template) is a ready-to-use GitHub repository designed to help you create, customize, and deliver hands-on enablements using GitHub Codespaces. It provides a robust starting point for trainers, solution architects, and educators to build interactive learning environments with minimal setup.

---


### 🚀 What is the Codespaces Template?


This template repository offers:

- A pre-configured `.devcontainer` for instant Codespaces launches
- Example documentation and structure for enablement content
- GitHub Actions for CI/CD and documentation deployment
- Integration with Dynatrace and other cloud-native tools
- A clean starting point for your own enablement projects

---

### 📦 Repository Overview

**Main features:**

- **.devcontainer/**: All configuration for Codespaces and local dev containers
- **docs/**: MkDocs-based documentation, ready to extend
- **.github/workflows/**: CI/CD for integration tests and GitHub Pages deployment
- **README.md**: Project overview and quickstart
- **mkdocs.yaml**: Navigation and site configuration


For a complete file and folder breakdown, see the [repository on GitHub](https://github.com/dynatrace-wwse/enablement-codespaces-template).

---


### 📝 How to Use the Template


1. **Create your own enablement repository**
	- Click "Use this template" on [the GitHub repo](https://github.com/dynatrace-wwse/enablement-codespaces-template)
	- Name your new repository and clone it locally
2. **Customize the content**
	- Edit the `docs/` folder to add your enablement instructions, labs, and resources
	- Update `.devcontainer/devcontainer.json` to add dependencies or secrets as needed
3. **Launch in Codespaces**
	- Click the **Code** button in your repo and select "Open with Codespaces"
	- Your environment will be ready in seconds, with all tools and docs pre-installed
4. **Publish documentation**
	- Use the `installMKdocs` function to install MkDocs inside the container and serve the documentation locally on port 8000, making it easy to write and preview your documentation.
	- Push changes to `main` to trigger GitHub Pages deployment (see Actions tab)
	- Your docs will be live at `https://<your-org>.github.io/<your-repo>/`

---

### 📝 TODOs in the Codebase

Throughout the template repository, you will find `TODO` comments in various files. These guide you step-by-step as you create your own enablements—reminding you where to add content, configure secrets, or customize scripts.

**Tip:**
To make working with TODOs easier, install a TODO highlighting extension in VS Code, such as [TODO Highlight](https://marketplace.visualstudio.com/items?itemName=wayou.vscode-todo-highlight) or [TODO Tree](https://marketplace.visualstudio.com/items?itemName=Gruntfuggly.todo-tree). These extensions help you quickly find and manage all TODOs in your project.

By following and resolving these TODOs, you can efficiently adapt the template to your specific enablement scenario.

---


### 🧑‍🏫 Who is this for?

- Trainers and educators creating hands-on labs
- Solution architects building demo environments
- Anyone seeking a fast, reproducible Codespaces-based enablement


---


### 📚 Documentation & Resources

- [Template Repository](https://github.com/dynatrace-wwse/enablement-codespaces-template)
- [How to use the Codespaces Template](https://dynatrace-wwse.github.io/enablement-codespaces-template/)


---

## App Training Template {#app-training-template}

!!! abstract "The [Enablement App Training Template](https://github.com/dynatrace-wwse/enablement-app-training-template)"
    The [Enablement App Training Template](https://github.com/dynatrace-wwse/enablement-app-training-template) is a trainer-authoring scaffold for building **interactive, scored trainings** that run on the **Orbital Operations server** and render inside the **Dynatrace app**. A trainer who follows the template in order can have a working interactive lesson — shell checks, kubectl, DQL validation, and a scored assessment — within approximately 30 minutes.

---

### 🚀 What is the App Training Template?

This template is designed for content authors who want to deliver training **inside the Dynatrace product itself**, not through a separate browser tab. Learners interact with a live Kubernetes cluster and their Dynatrace tenant, get immediate feedback on every step, and receive a final scored assessment — all within the Dynatrace app.

The template exposes every interactive block type as a live, runnable example:

| Block type | What it does |
|---|---|
| `shell-verification` | Runs a shell command in the Orbital container and validates the output |
| `kubectl` (interactive) | Terminal tab — learner types commands against the live cluster |
| `kubectl` (non-interactive) | `shell-verification` with jsonpath/grep patterns |
| `dql-verification` | Runs a DQL query against the learner's Dynatrace tenant |
| `STEP_SETUP` | Runs framework functions before a page renders (credential loading, DynaKube generation) |
| `multiple-choice` | Inline knowledge check (no separate file needed) |
| `boundScenarioId` | Links a full scored assessment from `.assessment/*.json` |
| Custom functions | `.devcontainer/util/my_functions.sh` — fault injection, scenario setup, validation helpers |
| `hs-video` | Embedded video from the Orbital server |
| `dt-app` deep links | In-lesson buttons that open Dynatrace apps (Kubernetes, Services, Notebooks, etc.) |

---

### 📦 Repository Overview

- **.assessment/**: Scored assessment JSON files (MCQ + DQL questions, points, hints)
- **.devcontainer/**: devcontainer config, `post-create.sh`, `my_functions.sh`
- **docs/**: 5-lesson trainer guide + `AUTHORING.md` + `ORBITAL_AND_APP.md` + `REFERENCE_KUBERNETES_101.md`
- **mkdocs.yaml**: Nav and site config
- **README.md**: Trainer quickstart

---

### 📝 How to Use the Template

1. **Create your training repository**
	- Click "Use this template" on [the GitHub repo](https://github.com/dynatrace-wwse/enablement-app-training-template)
	- Name your new repository `enablement-<topic>` in the `dynatrace-wwse` org
2. **Open in Codespaces and follow the 5-lesson guide**
	- `00 — Getting Started` → understand Orbital + Dynatrace app architecture
	- `01 — Lesson Anatomy` → block types, lifecycle, nav registration
	- `02 — Interactive Building Blocks` → click through every live example
	- `03 — Example Lesson` → see a complete lesson end-to-end
	- `04 — Publishing & Validation` → deploy to GitHub Pages, register with Orbital
3. **Replace example content** with your training topic
4. **Publish to GitHub Pages** with `deployGhdocs`
5. **Register with Orbital** — contact the Orbital administrator with your repo URL

---

### 🧑‍🏫 Who is this for?

- Solutions Engineers building scored interactive trainings inside the Dynatrace app
- Content authors who want to use the reference training (`enablement-kubernetes-101`) as a pattern without reverse-engineering it
- Trainers delivering Orbital-hosted labs with live cluster access and Dynatrace tenant integration

---

### 📚 Documentation & Resources

- [Template Repository](https://github.com/dynatrace-wwse/enablement-app-training-template)
- [Trainer Guide (GitHub Pages)](https://dynatrace-wwse.github.io/enablement-app-training-template/)
- [Authoring Reference](https://dynatrace-wwse.github.io/enablement-app-training-template/AUTHORING/)
- [Orbital & App Runtime](https://dynatrace-wwse.github.io/enablement-app-training-template/ORBITAL_AND_APP/)
- [Reference Training (kubernetes-101)](https://github.com/dynatrace-wwse/enablement-kubernetes-101)
- [Orbital Platform docs](ops-platform.md)

---

<div class="grid cards" markdown>
- [Let's continue :octicons-arrow-right-24:](framework.md)
</div>
