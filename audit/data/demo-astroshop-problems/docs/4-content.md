--8<-- "snippets/4-content.js"


## Accessing the Astroshop

### in Kubernetes




### in the UI


[http://localhost:30100](http://localhost:30100)



## Triggering Problems

### Manually via UI

Go to the codespaces exposed port, since the astroshop is the first app deployed, the assigned port is 30100. THe url for the features flag should look something like [http://localhost:30100/feature](http://localhost:30100/feature)

![features flag](img/features_flag.png)


### Manually via REST API

### Scheduling Problems

### 

```bash
Fraud Detection Service gets OOM Kill, reason:

dynatrace-operator to create fsnotify watcher: too many open files


sudo sysctl -w fs.inotify.max_user_watches=524288
sudo sysctl -w fs.inotify.max_user_instances=512
sudo sysctl -w fs.inotify.max_queued_events=16384


echo fs.inotify.max_user_watches=524288 | sudo tee -a /etc/sysctl.conf
echo fs.inotify.max_user_instances=512 | sudo tee -a /etc/sysctl.conf
echo fs.inotify.max_queued_events=16384 | sudo tee -a /etc/sysctl.conf
sudo sysctl -p


```



<div class="grid cards" markdown>
- [Let's continue:octicons-arrow-right-24:](cleanup.md)
</div>
