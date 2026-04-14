**Content Security Policy CSP**

You may encounter sites that restrict remote content loading and requests using the [Content Security Policy (CSP)](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/CSP){target=_blank}. 
While the new version of the RUM injector should resolve these issues, the below details are included if you run into problems.

Content Security Policy (CSP) is a feature that helps to prevent or minimize the risk of certain types of security threats. It consists of a series of instructions from a website to a browser, which instruct the browser to place restrictions on the things that the code comprising the site is allowed to do.

The primary use case for CSP is to control which resources, in particular JavaScript resources, a document is allowed to load. This is mainly used as a defense against [cross-site scripting (XSS)](https://developer.mozilla.org/en-US/docs/Glossary/Cross-site_scripting){target=_blank} attacks, in which an attacker is able to inject malicious code into the victim's site.

Since we are injecting our RUM locally via an extension, you will see these warnings in your Chrome Developer Tools Console.

### Configure CSP Overrides

!!! warning "Overrides Active with Developer Tools Only"
    Enabling these local overrides only function while the Chrome Developer Tools are open.  Be sure to leave them open for the entire time that you're doing this exercise and browsing the website.

Navigate to the website (you should be there already).

Open the Developer Tools `View > Developer > Developer Tools`. Switch to the `Sources` tab, then `Overrides` sub-tab.

Click `Select folder for overrides`.  Navigate to the `overrides` directory **in your unzipped extension folder** and press `Select`.

If prompted to allow local edit, click `Edit`.

![Add Local Overrides](./img/real-user_configure_extension_add_local_overrides.gif)

Click and hold the refresh button and select `Empty cache and hard reload`.  As the page loads, navigate to the `Network` tab of Developer Tools and locate the Dynatrace RUM beacons.  You can filter on `Fetch/XHR` request types and the pattern `bf?`.  Validate that you're seeing HTTP status **200**.
