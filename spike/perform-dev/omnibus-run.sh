# Omnibus CE, GitLab "running in a memory-constrained environment" settings. Root password: generated, never printed.
pw=$(head -c 18 /dev/urandom | base64 | tr -d '/+=' | head -c 24)
printf '%s' "$pw" > /root/omni-pw; chmod 600 /root/omni-pw; unset pw
cfg="external_url 'https://gitlab-omni--spike92.staging.autonomous-enablements.whydevslovedynatrace.com'; nginx['listen_port']=80; nginx['listen_https']=false; letsencrypt['enable']=false; puma['worker_processes']=0; sidekiq['concurrency']=10; prometheus_monitoring['enable']=false; gitlab_rails['env']={'MALLOC_CONF'=>'dirty_decay_ms:1000,muzzy_decay_ms:1000'}; gitaly['env']={'MALLOC_CONF'=>'dirty_decay_ms:1000,muzzy_decay_ms:1000'}; registry['enable']=false; gitlab_kas['enable']=false; gitlab_pages['enable']=false; mattermost['enable']=false"
date +%s > /root/omni-t0
docker run -d --name omni -p 8929:80 --shm-size 256m -e GITLAB_ROOT_PASSWORD="$(cat /root/omni-pw)" -e GITLAB_OMNIBUS_CONFIG="$cfg" gitlab/gitlab-ce:18.4.0-ce.0 >/dev/null && echo started
