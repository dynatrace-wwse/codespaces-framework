# Wrap Up

--8<-- "snippets/send-bizevent/4-wrap-up.js"

### What You Learned Today 
You explored how to connect data using the `append`, `data`, `lookup`, and `join` commands in DQL to combine data from multiple sources. 

Understanding `data`:
* The `data` command allows you to add any data into a Notebook, Dashboard, Workflow, or other DQL components.
* It's useful for pulling in data from other sources like Excel or CSV files.
* JSON data types are allowed.

You explored an example of nested JSON and learned how to parse it using `fieldsFlatten` and `fieldsKeep`.

Adding Data Sets with `append`:
* The append command behaves similarly to a SQL UNION ALL operation, combining two data sets.
* You learned how to use append to combine metrics and logs on the same entity.

Understanding `lookup`:
* The `lookup` command adds fields from a subquery (lookup table) to the source table by matching fields.
* It mimics a SQL INNER JOIN but only returns the first matching result.
* Null values in key fields are not returned.
* The lookup function nests all included fields as a record.

In addition, you explored the entity model with `describe`. The `describe` command helps you understand the data model fields present for a given entity. You can use the Dynatrace semantic dictionary to map relationships between keys in different observability facets.

A summary of the common topologies for application, service, process, and host entities was provided to help you understand relationships in the Dynatrace data model.

You also delved into the `entityAttr` command in DQL, which allows you to traverse the Dynatrace data model for a given entity on a particular field. Often, you may need to use `describe` to understand the fields available for the data object to be suitably used in `entityAttr`. This command enriches your queries and allows you to pass intents between apps.  

Finally, you explored the `join` command in DQL. 

Understanding `join`:

* The join command adds fields from a subquery (set `B`, the "right") to the source table (set `A`, the "left").
* There are three types of joins: `inner`, `leftOuter`, and `outer`.

Types of Joins:

* Inner Join (kind: inner): Returns only the matching records from both tables.
* Left Outer Join (kind: leftOuter): Returns all records from the left table and the matching records from the right table. Non-matching records from the right table are returned as null.
* Outer Join (kind: outer): Returns all matching and non-matching records from both tables.

By mastering these concepts, you can effectively connect and analyze data in Dynatrace, enhancing your observability and monitoring capabilities. Keep practicing with the provided exercises to solidify your understanding! 🚀

--8<-- "snippets/feedback.md"
