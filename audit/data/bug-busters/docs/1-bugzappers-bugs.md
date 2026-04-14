--8<-- "snippets/bugzappers-bugs.js"

## Bug 1: Why are the top scores not being cleared?
There are a few bugs in the Bugzapper app and your mission is to find them by investigating the application and using Dynatrace to help your investigation.

Open the bugzappers game in your browser (if its not open, go to the codespaces 'Ports' tab and open the app on port 30200 in your browser)

To start, play a game to make sure there are some top scores on the scoreboard:

![Bug Zapper](img/bugzapper-start.png)

**Hints**

- Try to clear the scores from the Top Scores. What do you notice?
- Try to use the Distributed Tracing App to understand which API calls are being made. Filter on the `asteroids-game` service. Press 'ctrl/cmd + K' in Dynatrace and type 'Distributed Tracing' to find the app.

![Bug Service](img/bugzapper-service.png)

- Use the Live Debugger to set a breakpoint in the part of the code that's responsible for clearing the scores. Press 'ctrl/cmd + K' in Dynatrace and type 'Live Debugger' to find the app. Click the purple pencil icon to set a Live Debugger filter. Use the `bugzapper` namespace as the filter. The source code repository should populate automatically. 

## Bug 2: Why are the past game stats not showing up correctly?
Now that you've played a game, you can view your game stats by clicking on the `View Game Stats` button.

![Bug Zapper Stats](img/bugzapper-game-stats.png)

Now click on `Past Game Stats` to view the past game stats. What do you notice?

***Hints***

- Try to use the Distributed Tracing App to understand which API calls are being made. Filter on the `asteroids-game` service.
- Go to the Asteroids Game service in the `Services` app and check out the Logs. Notice there are some failures. Press 'ctrl/cmd + K' in Dynatrace and type 'Services' to find the app
- Based on the error logs, use the Live Debugger to set a breakpoint in the part of the code that is responsible for storing the game stats when a game ends.

Did you find the bugs? Great job. Let's move on to the next app.

<div class="grid cards" markdown>
- [Let's Find More Bugs in the Todo App:octicons-arrow-right-24:](2-todoapp-bugs.md)
</div>
