#!/bin/bash

# VARIABLES DECLARATION
if [ -n "$FRAMEWORK_CACHE" ]; then
  source "${FRAMEWORK_CACHE}/.devcontainer/util/variables.sh"
else
  source "$REPO_PATH/.devcontainer/util/variables.sh"
fi

printDynatraceLogo(){

    echo -e "${thickline}"
    echo -e ""
    echo -e "      ${CYAN}                 Welcome to your your Dev Container                ${RESET}                "
    echo -e "       This enablement was made with ${RED}${HEART}${RESET} from the Dynatrace SE Center of Excellence Team                                                                                             "
    echo -e "${GREEN} "
    echo -e "      ${CYAN}.oyyyyyson+${GREEN}.          sh                               hs                                                         "
    echo -e "  ${CYAN}.:yhhhhhhhhh/ ${GREEN}oy.   .:HHHHhd /:      /: .:HHH:.   :mHHHm.  dh//-  .mmmm. -mHHHm:   -HHHH.   .:HHHH:.                  "
    echo -e "  ${BLUE}s.${CYAN}  PPPPPPP ${GREEN}nhhh   od/----yd /m:    sd./d+:  \dy       :do dh    .ms          :do dy:      sd/     d+                 "
    echo -e "  ${BLUE}hhh.       ${GREEN}ohhhh-  m+     sd  om-  +m- hh     oN. -:mmm:ym dy    -N/    -:mmmm:ym N.       my:mmmdh*                  "
    echo -e "  ${BLUE}hhh        ${GREEN}shhhh:  m+     sd   sd./m/  hh     oN-hh:    ym dy    -N/   .ds    -ms N.       my                         "
    echo -e "  ${BLUE}hy ${LILA}::::::: ${GREEN}yhhho   od/---:dh    hdm+   hh     oN-dh:    hh yd:-- -N/   .mo    :mo dy:----  sd:                        "
    echo -e "  ${BLUE}/ ${LILA}yhhhhhhh-${GREEN}hh+.     .:HHHH:     -Ns    hh     oN  :HHHHH:   :HHH:-N/    .HHHHHH:   -HHHHH.   *HHHH*                   "
    echo -e "   ${LILA}.osyyhhhh+${GREEN}/*                   yy                                                                                    "
    echo -e "${NORMAL}                                                                                                               "
    echo -e "${thickline}"                                                                       
    echo -e "  ${CYAN}   General System Information of your dev.container          ${RESET}             "
    echo
    echo -e " ${LILA}OS & Kernel Version    ${NORMAL}       "
    uname -a
    echo
}

printKubernetesInformation(){
    if [[ "$CLUSTER_STATUS" == "running" ]]; then
        echo -e " ${LILA}Kubernetes Cluster (${CLUSTER_TYPE}) ${NORMAL}       "
        kubectl version
    else
        echo -e " ${YELLOW}${WARNING}${ORANGE} No Kubernetes Cluster is running ${NORMAL}       "
        echo -e "   ${RESET}startCluster${NORMAL} will start, create or attach to a running Cluster (engine: ${CLUSTER_ENGINE})  "
        echo -e "   ${NORMAL}Change engine with: ${RESET}export CLUSTER_ENGINE=kind${NORMAL} or ${RESET}export CLUSTER_ENGINE=k3s${NORMAL}"
        echo -e "                                                                                                             "
    fi
    echo -e "${RESET}${thinline}"
    echo -e "                                                                                                             "
}

printCodespacesInformation(){
    echo -e " ${LILA}GitHub Pages: ${RESET}https://dynatrace-wwse.github.io/${RepositoryName}    "
    echo -e " ${LILA}GitHub Repository: ${RESET}${GITHUB_REPOSITORY}     "
    
    if [[ -z $DT_ENVIRONMENT ]]; then
        echo -e " ${YELLOW}${WARNING}${ORANGE} No Dynatrace information provided."
    else
        echo -e " ${LILA}Dynatrace Environment: ${RESET}${DT_ENVIRONMENT}"
    fi
    
    echo -e "                                                                                                             "
    echo -e " ${LILA}Codespaces information: ${NORMAL}   "
    echo -e "Instantiation Type: ${RESET}${INSTANTIATION_TYPE}${NORMAL}    "
    echo -e "User: ${RESET}${USER}${NORMAL} @ ${RESET}${HOSTNAME}${NORMAL}"
    if [[ $CODESPACES == true ]]; then
        echo -e "Codespaces name ${RESET}${CODESPACE_NAME}${NORMAL} running for gh-user ${RESET}${PRINT_USER}    "
    fi
    # MCP Server status
    if [ -f "$REPO_PATH/.vscode/mcp.json" ]; then
        echo -e " 🧠 Dynatrace MCP Server: ${GREEN}enabled${NORMAL} — connected to ${RESET}${DT_ENVIRONMENT:-unknown}${NORMAL}"
        echo -e "    ${NORMAL}Type ${RESET}disableMCP${NORMAL} to disconnect or ${RESET}selectEnvironment${NORMAL} to switch"
    else
        echo -e " 🧠 Dynatrace MCP Server: ${YELLOW}not enabled${NORMAL}"
        echo -e "    ${NORMAL}Type ${RESET}enableMCP${NORMAL} to connect VS Code to Dynatrace"
    fi
}


printRunningApplications(){

    local running_app=false

    # Determine the run environment. greeting.sh runs as a standalone bash
    # process that sources only variables.sh — functions.sh (and
    # detectRunEnvironment) is NOT available here. Use INSTANTIATION_TYPE,
    # which variables.sh has already computed and which is authoritative:
    # it handles the combined Codespace+Orbital case AND the "is this
    # Codespace really mine?" downgrade (cached Orbital API verdict).
    #
    # Do NOT re-derive from ORBITAL_ENVIRONMENT here: that is a STICKY
    # repo-scoped Codespaces secret, so it reads "true" inside a plain,
    # hand-opened Codespace too. Classifying such a Codespace as "orbital"
    # left it with no orbital_subdomain and fell through to the magic-DNS
    # branch, printing unreachable sslip.io links.
    #
    #   orbital    → wildcard subdomain on autonomous-enablements.*
    #   codespaces → GitHub port forwarding (incl. orbital_codespaces: an
    #                Orbital-launched Codespace still serves apps this way,
    #                which is also what getAppURL concludes)
    #   local      → fallback (magic-DNS sslip.io)
    local _greet_env="local"
    case "${INSTANTIATION_TYPE:-}" in
        orbital)                              _greet_env="orbital"    ;;
        orbital_codespaces|github-codespaces) _greet_env="codespaces" ;;
    esac
    # Fallbacks, only when INSTANTIATION_TYPE gave nothing (missing .env, or a
    # cached older variables.sh). The master-* cluster prefix is the Orbital
    # worker's port-override pattern — the same extra signal detectRunEnvironment
    # uses, which INSTANTIATION_TYPE does not carry.
    if [[ "$_greet_env" == "local" ]]; then
        if [[ "${K3D_CLUSTER_NAME:-}" == master-* ]]; then
            _greet_env="orbital"
        elif [[ -n "${CODESPACE_NAME:-}" ]]; then
            _greet_env="codespaces"
        fi
    fi

    # Apps are exposed via ingress — show them from the registry, with the URL
    # form matching the run environment.
    if [[ -f "$APP_REGISTRY" ]] && [[ -s "$APP_REGISTRY" ]]; then
        local _fwd_domain="${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"
        while IFS='|' read -r app_name namespace service_name service_port ingress_host cs_port orbital_subdomain; do
            running_app=true
            if [[ "$_greet_env" == "orbital" && -n "$orbital_subdomain" ]]; then
                echo -e "${CYAN}   $app_name ${NORMAL}is reachable under ${RESET}https://${orbital_subdomain}.autonomous-enablements.whydevslovedynatrace.com"
            elif [[ "$_greet_env" == "codespaces" ]]; then
                # Field 6 of the registry is the forwarded port: empty for apps
                # served by the :80 ingress catch-all, 8000 for mkdocs.
                echo -e "${CYAN}   $app_name ${NORMAL}is reachable under ${RESET}https://${CODESPACE_NAME}-${cs_port:-80}.${_fwd_domain}"
            elif [[ -n "$ingress_host" ]]; then
                echo -e "${CYAN}   $app_name ${NORMAL}is reachable under ${RESET}http://${ingress_host}"
            else
                # No host for this environment (e.g. a row written for
                # Codespaces read back elsewhere) — say so rather than printing
                # a bare "http://".
                echo -e "${CYAN}   $app_name ${NORMAL}${NORMAL}has no URL for this environment${RESET}"
            fi
        done < "$APP_REGISTRY"
    fi

    if [[ $running_app == false ]]; then
        echo -e "   ${NORMAL}No applications are running, to list the app repository ${PACKAGE} type ${RESET}deployApp${NORMAL}"
    else
        echo -e "   ${NORMAL}For managing your apps ${PACKAGE} type ${RESET}deployApp${NORMAL} or ${RESET}listApps${NORMAL}"
    fi
}

printApplications(){
    echo -e "                                                                                                             "
    echo -e " ${LILA}Running applications in your Kubernetes Cluster: ${NORMAL}   "
    if [[ "$CLUSTER_STATUS" == "running" ]]; then
        printRunningApplications
        echo -e "                                                                                                             "
    else
        echo -e "${YELLOW}${WARNING}${ORANGE} First start the Kubernetes cluster. ${RESET} "
        echo -e "                                                                                                             "
    fi
}


printCodespacesVerification(){
    echo -e "${CYAN}This container has the following tools installed and configured for your best experience:${RESET} "
    echo -e "  ${RESET}k9s kubectl helm node npm jq python3 gh zsh k3d kind p10k ${RESET} "
    echo -e "                                                                                                             "
    echo -e "${CYAN}If you want to make the endpoints public accesible, just go to the ports section in VsCode, right click on them and change the visibility to public ${NORMAL}"
    echo -e "${CYAN}When you are finished with your codespace, you can comfortably delete it by typing in the Terminal${RESET} deleteCodespace"
    echo -e "                                       " 
    if [ "$ERROR_COUNT" -gt 0 ]; then
        echo -e "${RED} There has been $ERROR_COUNT errors detected in the creation of the codespace, type ${RESET}verifyContainerCreation${RED} to understand more. ${RESET}                          " 
    else
        echo -e "${GREEN} There has been no errors detected in the creation of the codespace. ${RESET}                          " 
    fi
    echo -e "${thinline}"
}


# ══════════════════════════════════════════════════════════════════════════════
#  Orbital greeting — shown when the container runs inside the Dynatrace
#  Enablement App. Deliberately short: the learner already has the training in
#  a browser tab, so anything the tab shows is noise in the terminal.
# ══════════════════════════════════════════════════════════════════════════════

printOrbitalGreeting(){
  local title tenant
  # variables.sh derives the title from mkdocs.yaml — shared with the p10k prompt.
  title="${DT_TRAINING_TITLE:-${RepositoryName:-Dynatrace Enablement}}"
  tenant="${DT_ENVIRONMENT:-}"

  echo -e "${thinline}"
  echo -e "${GREEN} "
  echo -e "      ${CYAN}.oyyyyyson+${GREEN}.          sh                               hs                                                         "
  echo -e "  ${CYAN}.:yhhhhhhhhh/ ${GREEN}oy.   .:HHHHhd /:      /: .:HHH:.   :mHHHm.  dh//-  .mmmm. -mHHHm:   -HHHH.   .:HHHH:.                  "
  echo -e "  ${BLUE}s.${CYAN}  PPPPPPP ${GREEN}nhhh   od/----yd /m:    sd./d+:  \dy       :do dh    .ms          :do dy:      sd/     d+                 "
  echo -e "  ${BLUE}hhh.       ${GREEN}ohhhh-  m+     sd  om-  +m- hh     oN. -:mmm:ym dy    -N/    -:mmmm:ym N.       my:mmmdh*                  "
  echo -e "  ${BLUE}hhh        ${GREEN}shhhh:  m+     sd   sd./m/  hh     oN-hh:    ym dy    -N/   .ds    -ms N.       my                         "
  echo -e "  ${BLUE}hy ${LILA}::::::: ${GREEN}yhhho   od/---:dh    hdm+   hh     oN-dh:    hh yd:-- -N/   .mo    :mo dy:----  sd:                        "
  echo -e "  ${BLUE}/ ${LILA}yhhhhhhh-${GREEN}hh+.     .:HHHH:     -Ns    hh     oN  :HHHHH:   :HHH:-N/    .HHHHHH:   -HHHHH.   *HHHH*                   "
  echo -e "   ${LILA}.osyyhhhh+${GREEN}/*                   yy                                                                                    "
  echo -e "${RESET}"
  echo -e "  ${CYAN}${title}${RESET}"
  # Literal UTF-8 glyphs, not $HEART/$WARNING: those are stored as the six
  # characters ♥ and bash's echo -e does not expand \uHHHH here, so the
  # shared vars print raw escapes (visible in the legacy greeting too).
  echo -e "  ${NORMAL}Delivered through the Dynatrace Enablement App — made with ${RED}♥${NORMAL} by the SE Center of Excellence${RESET}"
  echo -e ""

  # No Cluster line: K3D_CLUSTER_NAME is not set in an Orbital Sysbox container
  # (verified live), so it could only ever have rendered blank.
  [ -n "$tenant" ] && echo -e "  ${LILA}Tenant${RESET}    ${tenant}"

  # Registered apps. Field 1 of the app registry is the app name; the URL is
  # deliberately not printed — the learner reaches the app through its own tab.
  if [ -f "$APP_REGISTRY" ] && [ -s "$APP_REGISTRY" ]; then
    local app_name
    while IFS='|' read -r app_name _rest; do
      [ -n "$app_name" ] || continue
      echo -e "  ${LILA}App${RESET}       ${CYAN}${app_name}${RESET} has been registered in your workspace"
    done < "$APP_REGISTRY"
  fi

  echo -e ""

  echo -e "  ${NORMAL}For your best dev experience: ${RESET}k9s kubectl helm k3d node npm jq python3 gh${RESET}"

  # No MCP line here on purpose: the MCP server needs VS Code running with an
  # agent attached to it, and a plain Orbital container has neither. Advertising
  # enableMCP would point the learner at something that cannot work yet.

  if [ "${ERROR_COUNT:-0}" -gt 0 ] 2>/dev/null; then
    echo -e "  ${YELLOW}⚠${ORANGE} ${ERROR_COUNT} errors detected while creating your environment${RESET} — type ${RESET}verifyContainerCreation${NORMAL} for details${RESET}"
  else
    echo -e "  ${GREEN}✔${RESET} Environment ready — no errors detected."
  fi
  echo -e "${thinline}"
}


# ── Dispatch ──────────────────────────────────────────────────────────────────
# Orbital (the Dynatrace Enablement App) gets its own minimal greeting: the
# learner is inside an app tab, so the GitHub URLs, the app URL, the slot
# hostname, the VS Code port advice and deleteCodespace are all either wrong or
# already on screen. Every other instantiation type keeps the full greeting.
#
# "orbital_codespaces" is deliberately NOT routed here, even though the name
# looks like it belongs. It is a real GitHub Codespace that Orbital merely
# launched, so every reason above is false for it:
#   - there IS a VS Code, so the port-visibility advice applies;
#   - GitHub, not Orbital, owns the lifecycle, so deleteCodespace is correct;
#   - the hostname is the learner's own Codespace, not an internal slot name;
#   - the app is served by GitHub port forwarding, not by an Orbital wildcard
#     subdomain — which is exactly what getAppURL and printRunningApplications
#     both already conclude (see the "codespaces" arm above).
# Routing it here stripped the app URL out of every Orbital-launched Codespace,
# and out of every hand-opened one that kept the type via the variables.sh
# fail-safe (ops server unreachable / no curl).
case "${INSTANTIATION_TYPE:-}" in
  orbital)
    printOrbitalGreeting
    ;;
  *)
    printDynatraceLogo
    printKubernetesInformation
    printCodespacesInformation
    printApplications
    printCodespacesVerification
    ;;
esac
