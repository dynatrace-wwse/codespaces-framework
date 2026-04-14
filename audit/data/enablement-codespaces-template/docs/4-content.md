--8<-- "snippets/4-content.js"

## The Dynatrace Codespaces Enablement Framework
For understanding deeply how the framework works, how to run it locally, the different types of instantiations, integration tests, github actions and more, please read the following documentation: 
[https://dynatrace-wwse.github.io/codespaces-framework](https://dynatrace-wwse.github.io/codespaces-framework)


## Enablement content

In here you put your enablement content after Codespaces has been started and everything is ready and set-up.


## How MKdocs work

All the MD files are under the **docs** folder. In the main README.md file you want to give an intro on what the tutorial is about, being very short and to the point. If you add images there you reference them to doc/img folder.


## MKDocs reference

Visit this page for the reference on using MKDocs markdown:

[https://squidfunk.github.io/mkdocs-material/reference/](https://squidfunk.github.io/mkdocs-material/reference/)


### Snippets

Snippets allow you to reuse text, banners, code or pieces of code.

This is a snippet with an admonition:

```bash

--8<-- "snippets/view-code.md"

```

--8<-- "snippets/view-code.md"

### Admonitions

This is a warning admonition
```bash
!!! warning "Warning"
    This is a Warning 
```
looks like:
!!! warning "Warning"
    This is a Warning 

This are the available admonitions added with a snippet:

--8<-- "snippets/admonitions.md"

### The relation between the mkdocs.yaml file, the md files and the javascript files (BizEvents) in the snippets folder.
The menu on the left hand side is defined in `mkdocs.yaml`. The first page needs to be called index.md, You can call it whatever you want, in our case we call it About. The name from the mkdocs.yaml file will be set as title as long as you add in the same .md file a js file.

Example the ```index.md``` file has at the top a snippet ```--8<-- "snippets/index.js"```

!!! warning "Important"
    This is because we want to monitor the usage and adoption of the Github pages of your training and since we are using agentless rum, we need to add this to each page. in the JS file we add the same name we defined in the Menu Navigation in the mkdocs.yaml file for having consistency. This way we can understand the engagement of each page, the time the users spent in each page so we can improve our trainings.

As a best practice we recommend for each MD file have a JS file with the same name, and this should be reflected in the mkdocs.yaml file. 

Meaning before going live, after you have created all your MD files, make sure that:
- each page.md file has a snippet/page.js file associated with it
- the page.js file inside reflects the same name as in the mkdocs.yaml file, so RUM reflects the page the user is reading.

### Headings in MKDocs
if you start the md file with a snippet, automatically it'll take the name defined in the mkdocs file. You can override it by adding a Heading1 # which is only one #. For example this page is overriding the heading. As you can see there is no number 4 in the Content. All H2, H3 and so forth will be shown on the right pane for enhanced navigation and better user experience.


### Writing live the MKDocs
This codespace has in the `post-create.sh` the function `installMkdocs` which installs `runme` and the `mkdocs` and will expose any changes live in port 8000. The function `exposeMkdocs` will publish automatically the mkdocs so you can see in an instant the changes. 
Before going live you should comment out the function from the `post-create.sh` file for two reasons:

1.- you'll improve the rampup time of all the containers created for your session and 

2.- you dont want your users to go to the local copy of the labguide but to the one in the internet so we can monitor all user interactions with your lab guide. 

To watch the local mkdocs just go to the terminal and see the process exposed in port 8000.


### Deploying the Github pages.
For this you'll need write access to the repo where the enablement is being hosted. There is a function loaded in the shell called `deployGhdocs`. Before calling it, be sure that you have commited your changes to the branch you are working on.
When you call it it should look something like this:

```bash
@sergiohinojosa ➜ /workspaces/enablement-codespaces-template (main) $ deployGhdocs 
INFO    -  Cleaning site directory
INFO    -  Building documentation to directory: /workspaces/enablement-codespaces-template/site
INFO    -  The following pages exist in the docs directory, but are not included in the "nav" configuration:
             - snippets/admonitions.md
             - snippets/disclaimer.md
             - snippets/grail-requirements.md
             - snippets/view-code.md
INFO    -  Documentation built in 0.31 seconds
INFO    -  Copying '/workspaces/enablement-codespaces-template/site' to 'gh-pages' branch and pushing to GitHub.
Enumerating objects: 61, done.
Counting objects: 100% (61/61), done.
Delta compression using up to 2 threads
Compressing objects: 100% (21/21), done.
Writing objects: 100% (33/33), 1.31 MiB | 6.24 MiB/s, done.
Total 33 (delta 15), reused 1 (delta 0), pack-reused 0 (from 0)
remote: Resolving deltas: 100% (15/15), completed with 12 local objects.
To https://github.com/dynatrace-wwse/enablement-codespaces-template
   bca482c..db42f7f  gh-pages -> gh-pages
INFO    -  Your documentation should shortly be available at: https://dynatrace-wwse.github.io/enablement-codespaces-template/

```

Make sure that there is no warning in there, if there is a warning is because most likely you are referencing a page or an image that is missing or wrong.

### Automatic deployment of the Github pages.
There is a gitHub workflow that when a Pull Request is merged into main, automatically the changes in the `doc` folder and all the documentation as specified in MKdocs, will be automatically published into the github pages and is served from the branch `ghpages`.

### Protection of the Main Branch
The main branch is protected, no pushes can be done directly to the main branch. For adding features or changes to a repo first create a branch, something like `rfe/feature1` or `fix/docs` so from the naming users can understand what the change is about. Then create a Pull Request. When a Pull Request is created, integration tests will run. If the test run succesfully, then the merge can be done. We recommend to delete the branch after is merged so we keep the repo as clean as possible. 

## Adding apps and instantiating apps in your codespace
The architecture is done in a way that will help us troubleshoot issues faster and has a good separation of concerns. All the logic is found in the `functions.sh` file. So the best is to add the deployment of the app in there and then reference it in the `post-create.sh` or `post-start.sh` file. 

Now, the terminal loads this functions as well, this gives you the possibility to have interactive trainings. Let's say that you want to add an error, or block a firewall, anything, well you can add it in a function that the user can call `startTraining` or whatever we want to do. 


## Before Going Live
Make sure to install the plugin Todo Tree. This is a great plugin for tracking TODOs in repositories. I've added a couple of TODOs that you'll need to take care before going live. 


## Enhancing the Codespace template
For enhancing the documentation just create a Fork of this repo and add a PR.

If you want to change core files of the framework like in the `functions.sh` file or adding more apps, please create a [fork](https://github.com/dynatrace-wwse/codespaces-synchronizer/fork) in the [codespaces-synchronizer](https://github.com/dynatrace-wwse/codespaces-synchronizer/fork) repository which is the one synchronizing all repositories using the framework.


<div class="grid cards" markdown>
- [Let's continue:octicons-arrow-right-24:](cleanup.md)
</div>
