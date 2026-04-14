
.ConnectTimeoutException: Connect to sg-eu-west-1-54-77-175-7-acceptancee2e-euwest.sprint.dynatracelabs.com:443 [sg-eu-west-1-54-77-175-7-acceptancee2e-euwest.sprint.dynatracelabs.com/54.77.175.7]
endpoint='https://10.96.0.1:443', 



activegate 2025-04-07 12:47:18 UTC DEBUG   [<iid1110h>] [<collector.core>, LogStatisticsWriter] Stats for the last: 600 seconds. Local time: 2025-04-07 12:47:18 GMT. Threads: 645. Active agents: 2. Memory usage in kB: free memory: 30,924; heap: 116,736; max heap: 524,288; direct byte buffers: 871.                                                                    
activegate Message Statistics:                                                                                                                                                                                                                                                                                                                                                
activegate  AGENT_INCOMING:                 779; Errors:   0; Size:  844.6 KiB;  AGENT_OUTGOING:                 328; Errors:   0; Timeouts:   0; Size:   20.8 KiB;                                                                                                                                                                                                           
activegate  ANY_SERVER_INCOMING:             50; Errors:   0; Size:   28.8 KiB;  ANY_SERVER_OUTGOING:             50; Errors:   0; Timeouts:   0; Size:    4.3 KiB;                                                                                                                                                                                                           
activegate  SERVER_INCOMING:                730; Errors:   0; Size:   40.3 KiB;  SERVER_OUTGOING:               3640; Errors:   0; Timeouts:   0; Size:    3.3 MiB;                                                                                                                                                                                                           
activegate Network statistics:                                                                                                                                                                                                                                                                                                                                                
activegate  outgoingHttpErrors: 24;  incomingHttpErrors: 0                                                                                                                                                                                                                                                                                                                    
activegate Queue length: server Id: 3; OneWay: 53; Request: 0server Id: 4; OneWay: 55; Request: 1server Id: 6; OneWay: 51; Request: 0. Selfmonitoring: enabled: true; metrics sent: 2574.                                                                                                                                                                                     
activegate 2025-04-07 12:57:18 UTC DEBUG   [<iid1110h>] [<collector.core>, LogStatisticsWriter] Stats for the last: 600 seconds. Local time: 2025-04-07 12:57:18 GMT. Threads: 645. Active agents: 2. Memory usage in kB: free memory: 41,852; heap: 119,808; max heap: 524,288; direct byte buffers: 1,298.                                                                  
activegate Message Statistics:                                                                                                                                                                                                                                                                                                                                                
activegate  AGENT_INCOMING:                1494; Errors:   0; Size:    1.9 MiB;  AGENT_OUTGOING:                 600; Errors:   0; Timeouts:   0; Size:   36.8 KiB;                                                                                                                                                                                                           
activegate  ANY_SERVER_INCOMING:             50; Errors:   0; Size:   13.8 KiB;  ANY_SERVER_OUTGOING:             50; Errors:   0; Timeouts:   0; Size:    3.3 KiB;                                                                                                                                                                                                           
activegate  SERVER_INCOMING:               1049; Errors:   0; Size:   56.3 KiB;  SERVER_OUTGOING:               4697; Errors:   0; Timeouts:   0; Size:    4.7 MiB;                                                                                                                                                                                                           
activegate Network statistics:                                                                                                                                                                                                                                                                                                                                                
activegate  outgoingHttpErrors: 0;  incomingHttpErrors: 0                                                                                                                                                                                                                                                                                                                     
activegate Queue length: server Id: 3; OneWay: 53; Request: 0server Id: 4; OneWay: 55; Request: 1server Id: 6; OneWay: 51; Request: 0. Selfmonitoring: enabled: true; metrics sent: 5368.                                                                                                                                                                                     
activegate 2025-04-07 12:59:54 UTC INFO    [<iid1110h>] [<communication>, MessageBroker] First-chance exception org.apache.http.conn.ConnectTimeoutException: Connect to sg-eu-west-1-54-77-175-7-acceptancee2e-euwest.sprint.dynatracelabs.com:443 [sg-eu-west-1-54-77-175-7-acceptancee2e-euwest.sprint.dynatracelabs.com/54.77.175.7] failed: Connect timed out on http req
uest to URI 'https://sg-eu-west-1-54-77-175-7-acceptancee2e-euwest.sprint.dynatracelabs.com/communication', proxy: no proxy [Suppressing further messages for 10 minutes] [skipped logs: 10]                                                                                                                                                                                  
activegate 2025-04-07 13:07:18 UTC DEBUG   [<iid1110h>] [<collector.core>, LogStatisticsWriter] Stats for the last: 600 seconds. Local time: 2025-04-07 13:07:18 GMT. Threads: 645. Active agents: 2. Memory usage in kB: free memory: 52,378; heap: 119,808; max heap: 524,288; direct byte buffers: 1,670.                                                                  
activegate Message Statistics:                                                                                                                                                                                                                                                                                                                                                
activegate  AGENT_INCOMING:                1494; Errors:   0; Size:    1.9 MiB;  AGENT_OUTGOING:                 600; Errors:   0; Timeouts:   0; Size:   36.8 KiB;                                                                                                                                                                                                           
activegate  ANY_SERVER_INCOMING:             50; Errors:   0; Size:   21.0 KiB;  ANY_SERVER_OUTGOING:             50; Errors:   0; Timeouts:   0; Size:    3.3 KiB;                                                                                                                                                                                                           
activegate  SERVER_INCOMING:               1049; Errors:   0; Size:   56.3 KiB;  SERVER_OUTGOING:               4693; Errors:   0; Timeouts:   0; Size:    4.7 MiB;                                                                                                                                                                                                           
activegate Network statistics:                                                                                                                                                                                                                                                                                                                                                
activegate  outgoingHttpErrors: 5;  incomingHttpErrors: 0                                                                                                                                                                                                                                                                                                                     
activegate Queue length: server Id: 3; OneWay: 51; Request: 0server Id: 4; OneWay: 55; Request: 1server Id: 6; OneWay: 51; Request: 0. Selfmonitoring: enabled: true; metrics sent: 8156.                                                                                                                                                                                     
activegate 2025-04-07 13:09:41 UTC INFO    [<iid1110h>] [DalWatchClientAdapter] Watching for changes (/api/v1/events) timed out. Will reconnect with resourceVersion='3475': java.net.SocketTimeoutException: Read timed out [Suppressing further messages for 30 minutes] [skipped logs: 26]                                                                                 
activegate 2025-04-07 13:15:32 UTC WARNING [<iid1110h>] [ListWatchController] An error occurred in state WATCH. Will transition to next state in order to avoid data loss: configurationId='3814973623141514629', endpoint='https://10.96.0.1:443', resource='/api/v1/events' [Suppressing further messages for 30 minutes] [skipped logs: 2]                                 
activegate com.dynatrace.apiconnector.pipeline.stage.StageExecutionException: Error watch event received, stopping watch (The resourceVersion for the provided watch is too old.)                                                                                                                                                                                             
activegate     at com.dynatrace.apiconnector.k8s.dataaccess.DalWatchClientAdapter.handleNonLifecycleType(DalWatchClientAdapter.java:203)                                                                                                                                                                                                                                      
activegate     at com.dynatrace.apiconnector.k8s.dataaccess.DalWatchClientAdapter.watch(DalWatchClientAdapter.java:138)                                                                                                                                                                                                                                                       
activegate     at com.dynatrace.apiconnector.k8s.informer.ListWatchController.executeCurrentState(ListWatchController.java:157)                                                                                                                                                                                                                                               
activegate     at com.dynatrace.apiconnector.k8s.informer.ListWatchController.evaluateState(ListWatchController.java:135)                                                                                                                                                                                                                                                     
activegate     at com.dynatrace.apiconnector.k8s.informer.ListWatchController.run(ListWatchController.java:127)                                                                                                                                                                                                                                                               
activegate     at com.dynatrace.gen2.foundation.util.concurrency.api.LatchRunnable.run(LatchRunnable.java:34)                                                                                                                                                                                                                                                                 
activegate     at java.base/java.util.concurrent.ThreadPoolExecutor.runWorker(Unknown Source)                                                                                                                                                                                                                                                                                 
activegate     at java.base/java.util.concurrent.ThreadPoolExecutor$Worker.run(Unknown Source)                                                                                                                                                                                                                                                                                
activegate     at java.base/java.lang.Thread.run(Unknown Source)                                                                                                                                                                                                                                                                                                              
activegate                                                                                                                                                                                                                                                                                                                                                                    
activegate 2025-04-07 13:17:18 UTC DEBUG   [<iid1110h>] [<collector.core>, LogStatisticsWriter] Stats for the last: 600 seconds. Local time: 2025-04-07 13:17:18 GMT. Threads: 645. Active agents: 2. Memory usage in kB: free memory: 30,470; heap: 119,808; max heap: 524,288; direct byte buffers: 2,004.                                                                  
activegate Message Statistics:                                                                                                                                                                                                                                                                                                                                                
activegate  AGENT_INCOMING:                1494; Errors:   0; Size:    1.8 MiB;  AGENT_OUTGOING:                 600; Errors:   0; Timeouts:   0; Size:   36.8 KiB;                                                                                                                                                                                                           
activegate  ANY_SERVER_INCOMING:             50; Errors:   0; Size:   21.0 KiB;  ANY_SERVER_OUTGOING:             50; Errors:   0; Timeouts:   0; Size:    3.3 KiB;                                                                                                                                                                                                           
activegate  SERVER_INCOMING:               1049; Errors:   0; Size:   56.3 KiB;  SERVER_OUTGOING:               4693; Errors:   0; Timeouts:   0; Size:    4.6 MiB;                                                                                                                                                                                                           
activegate Network statistics:                                                                                                                                                                                                                                                                                                                                                
activegate  outgoingHttpErrors: 0;  incomingHttpErrors: 0                                                                                                                                                                                                                                                                                                                     
activegate Queue length: server Id: 3; OneWay: 54; Request: 0server Id: 4; OneWay: 55; Request: 1server Id: 6; OneWay: 51; Request: 0. Selfmonitoring: enabled: true; metrics sent: 10949.   



bash-4.4$ curl -k -v https://10.96.0.1:443
* Rebuilt URL to: https://10.96.0.1:443/
*   Trying 10.96.0.1...
* TCP_NODELAY set
* Connected to 10.96.0.1 (10.96.0.1) port 443 (#0)
* ALPN, offering h2
* ALPN, offering http/1.1
* successfully set certificate verify locations:
*   CAfile: /etc/pki/tls/certs/ca-bundle.crt
  CApath: none
* TLSv1.3 (OUT), TLS handshake, Client hello (1):
* TLSv1.3 (IN), TLS handshake, Server hello (2):
< 
* TLSv1.3 (IN), TLS app data, [no content] (0):
{
  "kind": "Status",
  "apiVersion": "v1",
  "metadata": {},
  "status": "Failure",
  "message": "forbidden: User \"system:anonymous\" cannot get path \"/\"",
  "reason": "Forbidden",
  "details": {},
  "code": 403
* Closing connection 0
* TLSv1.3 (OUT), TLS alert, [no content] (0):
* TLSv1.3 (OUT), TLS alert, close notify (256):



X change path to 'app'
- AG issues? Keep an eye
- Startup, timer needed for AG?
- On LD says 3 active instances... why?
- change image of IAM policies
- modify the Makefile of the app, make it cleaner
- Do build and push to own repo
- Expose port 8080 always
- map ingress to all * on app to port 8080 for having nigx RUM instrumentation
