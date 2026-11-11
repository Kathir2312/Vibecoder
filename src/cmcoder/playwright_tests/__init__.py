"""`/playwright`: set up Playwright tests for a web app, plan them, write them, run them and heal them.

    /playwright                       where things stand, and the next command
    /playwright setup [url] [-- start command]
    /playwright map                   every page the app has, as a checklist
    /playwright plan [area]           a test plan for an area of the app
    /playwright generate [area]       Playwright tests from that plan
    /playwright run [area]            run them; each failure is the test's or the app's
    /playwright heal [area]           fix the tests that are wrong; report what the app gets wrong

It is Playwright's own job and nothing else: it never changes the app, and nothing in it is
specific to a framework (a .NET MVC app, a Node app: anything that answers on a web address).
Fixing what the app gets wrong is an ordinary request afterwards.

cmcoder only prepares each step (this package): it looks at the project (probe.py), writes the
exact file contents the model is told to create (templates.py) and builds the step's request
(prompts.py, LangChain `PromptTemplate`s as in webtest.py); the model does the work with the tools
it has (Browser to look at the app, Write for files, Bash to run the tests).
"""
