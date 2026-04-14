--8<-- "snippets/2-bug-hunt-via-tracing.js"

## Open the Distributed Tracing App
!!! Tipp "Protip: Open the Tracing App anywhere in Dynatrace"
    Type CTRL + K, then search for 'Tracing'. The Tracing App should appear in the Super Search.
    ![TODO App](img/open_tracing_app.png)

!!! Tipp "Want to learn more about the Tracing App?"
    If you want to learn more about the [new Distributed Tracing App, watch this amazing 12 min recording of Dynatrace App Spotlights](https://www.youtube.com/watch?v=O4zWlwJ4hsA){target="_blank"}

In the filter add:

```text
"Kubernetes namespace" = todoapp AND "Kubernetes workload" = todoapp
```

You can also let the autocomplete help you or use the facets on the left-hand side to filter for all requests of the Pod `todoapp` that is deployed in the namespace `todoapp`.

![TODO App](img/tracing_app_filter.png)

If we take a look at the traces, we can see there is a trace named `addTodo`. By opening this trace, in the details pane on the right-hand side, we can see in the `Code Attributes` that the `Code function = addTodo` and the `Code Namespace = com.dynatrace.todoapp.TodoController`

Now we know where in the application code we should be looking for the bug!

## Open the Live Debugger

- Let's search for the `Code function = addTodo` under the `Code Namespace = com.dynatrace.todoapp.TodoController`. In the search,  type `TodoController` the class file appears, open it.
- Now let's search for the `AddTodo` function, the declaration is in line 22.

![AddToDo Method Source](img/todo_addtodo_method_source.png)

Notice anything unusual? The developer left a String function on line 26:

> `26` String todoTitle = newTodoRecord.getTitle().replaceAll("[^a-zA-Z0-9\\s]+", "");

- Let's add two breakpoints around that line, one before, let's say on line 23 and another on line 30.  

![AddToDo New Active Breakpoints](img/todo_addtodo_new_active_breakpoints.png)

- Go to the TodoApp in your browser and add a Task with a special character.

Task:
```text
This is exciting!!!
```

- Go back to the Live Debugger and watch the two snapshots, snapshots get gathered in real time.

![AddToDo Snapshots](img/todo_addtodo_snapshots.png)

- If you open the first snapshot, the one on line 23, you'll notice the Object `newTodoRecord.title = This is exciting!!!` that contains the exclamation mark. Meaning the data is being correctly passed on to the function `addTodo`, but then something happens and the `!!!` are removed.
- If you then look for the same attribute in the same method on the second snapshot you'll see that the `!!!` are gone.

## Watching variables
- We want to make your life as a developer easier. With the Live Debugger you can watch variables and see them change. For this right click on the  `newTodoRecord.title` and select `Watch`

![watching variables](img/ld_watch.png)

- You'll see that in the snapshots, the title variable captured in both snapshots are added for ease of debugging complex applications. This is a very simple app, but imagine you have hundreds or thousands of lines of code, not all of them written by you, using this strategy you can understand how specific variables change through the code.

![watching variables](img/ld_watch2.png)


## Fixing the bug and redeploying the app

Like the first bug, open in VS Code the class ``TodoController.java`` and apply your changes. For compiling and redeploying the app we a have comfort function for you that does the compilation and the redeployment in kubernetes for you. Give it a try!


```bash
redeployApp
```

Is the bug gone? Open the app and verify it!

Yet, another way of verifying you succeeded is by typing: 

```bash
is_bug2_solved
```

??? example "Solution for the bug Special Characters 🪲🛠️"

    Go to the terminal and type:
    
    ```bash
    solve_bug2
    ```

    This function will implement the bugfix from branch `solution/bug2`. 
    The function checkouts the code from `solution/bug2`, compiles the code and redeploys it into the Kubernetes cluster.

    <br>
    <details>
    <summary>🛠️ The code changes </summary>


    The solution for this bug is also simple. In the method `TodoController.addTodo` we saw how the title changed watching the variable with the live debugger. Most probably the developer was implementing something with regex and left the code there. If what we want is to keep the original title, we just need to comment out those lines.
    
    ```java
       
        newTodoRecord.setId(UUID.randomUUID().toString());
        logger.info("Adding a new todo: {}", newTodoRecord);
        // The bug in here in is for the bughunt example
        String todoTitle = newTodoRecord.getTitle().replaceAll("[^a-zA-Z0-9\\s]+", "");
        newTodoRecord.setTitle(todoTitle);
        todos.add(newTodoRecord);
               
    ```

    this way when the newTodoRecord is passed by from the request, the object is addeed as is to the `todo` array. The only field that is modified (or added) is the UUID.
    
    ```java
      
        newTodoRecord.setId(UUID.randomUUID().toString());
        logger.info("Adding a new todo: {}", newTodoRecord);
        // The bug in here in is for the bughunt example
        //String todoTitle = newTodoRecord.getTitle().replaceAll("[^a-zA-Z0-9\\s]+", "");
        //newTodoRecord.setTitle(todoTitle);
        todos.add(newTodoRecord);
            
    ```
    Commenting out those two lines is the solution, give it a try!

    </details>

    ??? question "Good to know about Version Control and Live Debugger"
        The `solve_bug2` function adds to the Kubernetes Deployment information to the Live Debugger where the source code resides. The solution is stored in the branch `solution/bug2`. 
        More on this in the section "Version Control" of this tutorial.



<div class="grid cards" markdown>
- [Click here to continue the quest with the next Bug:octicons-arrow-right-24:](3-bug-duplicate-task.md)
</div>
