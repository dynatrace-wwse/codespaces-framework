--8<-- "snippets/index.js"

--8<-- "snippets/disclaimer.md"

# MCP-Powered Security Analysis with Unguard

Welcome to this hands-on tutorial where you'll learn how to leverage the **Model Context Protocol (MCP)** with AI agents to analyze, detect, and remediate security vulnerabilities in a real-world application.

## 🎯 What You'll Learn

In this tutorial, you'll discover how to:

- **Connect to MCP Servers** and interact with AI agents through the Model Context Protocol
- **Analyze vulnerable code** in the Unguard application using AI-powered code analysis
- **Query Dynatrace insights** to correlate code vulnerabilities with runtime security findings
- **Interact with Davis CoPilot** for intelligent security recommendations
- **Automatically remediate vulnerabilities** with AI-assisted code fixes
- **Access observability data** including logs, metrics, traces, and security events

## 🔍 About Unguard

**Unguard** is an intentionally insecure cloud-native microservices application designed for security training and testing. It consists of eight microservices written in different languages (Java, .NET, Python, Node.js, PHP, Go, Next.js) and encompasses real-world vulnerabilities including:

- Server-Side Request Forgery (SSRF)
- SQL Injection
- Command Injection
- JWT Key Confusion
- Remote Code Execution (RCE)
- And many more security flaws

This makes it the perfect environment to learn how AI agents can help identify and fix security issues in production-like code.

## 🤖 The Power of MCP

The Model Context Protocol enables AI agents to:

- Access and analyze your codebase directly
- Connect to Dynatrace monitoring data in real-time
- Execute queries across logs, metrics, and security findings
- Provide actionable recommendations based on both code and runtime context
- Automate vulnerability remediation with precision

<p align="center">
  <img src="img/fixing_code.png" alt="AI-Powered Code Analysis" width="600">
</p>

## 🚀 What You'll Do

Throughout this tutorial, you will:

1. Connect to the MCP server using your credentials
2. Interact with the AI agent to explore the Unguard application
3. Identify security vulnerabilities in the code
4. Correlate code issues with runtime security findings from Dynatrace
5. Use the agent to automatically fix vulnerabilities
6. Generate executive summaries of security remediations

!!! tip "Ready to Start?"
    Let's begin by checking the prerequisites and connecting to the MCP server!

<div class="grid cards" markdown>
- [Check Prerequisites :octicons-arrow-right-24:](2-getting-started.md)
