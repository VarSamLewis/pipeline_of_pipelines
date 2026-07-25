Let's simplify the workflow for now

1st page:

- Upload page
- file upload from local (seperate input and target mapping uploads)
- client id dropdown with the ability to add new clients

In-transiion

- run command that sends the data to be embedded and then runs the mapping service

2nd page:

- Review mapping page
- expose the raw mapping json (non editable) but pretified to the user
- a confirm and a reject buttons to either rerun the mapping (don't worry about feedback for now) or accept it

In-transition:

- run codegen and code execution

3rd page:

- Review results page
- Have a csv viewer 100 rows at a time and a pagination option to get the next 100 (just make a round trip to the server)
- have a toggle to close the csv and instead view the code
- all are non mutable at this point
- have a confirm and reject that dp the same as the above page
