UI

- Done - Remove Reviewers from settings page
- Done - Client context on homepage - should just be client
- Done - Add panel to see client files
- Done - Make runs a seperate page and show all prev runs within an org
- Revamp entire to panel format UI

Applications
- Seperate roles - platform admin (just me), users (can see all clients), org admins (can edit api keys)   
- Clean code base
- Enforce organisation membership on UI routes, so invited users only access their organisation’s data.
- Provide reliable access revocation, including existing sessions.
 
Data Storage

- In cloud storage bucket create a clients/{client_name}/{file_name}\_timestamp.extension
- Validate that the seperation in the DB actually works

Infra

- Improve docker build step in CICD
- Seperate cloud run instance
- Remove seperate envs
- Cost optimisation
- Scale up DB server???
