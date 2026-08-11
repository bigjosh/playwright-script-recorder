> Historical note: this is the original design prompt the project started
> from. Details evolved during development (PNG capture baselines, alarm
> choices, diff viewer, immediate mode, ...) — see README.md for how the
> tool actually works today.

we are creating a tool that helps record scripts to be played back into a browser window over chrome remote debug port. 

Each recorded script is actually a pyhton program that uses (1) Playwright to interact with the target website, and (2) a small library PlaywrightScriptLib that we will create that simplifies and abstracts the functionality our scripts will need into a small and tight set of functions.

# playwrightscriptlib 

Needs the following functions:

connect( webSocketDebuggerUrl  ) - Connects to a browser already launched with chrome remote debug port active.
click( x ,y ) - coocrds are relative to upper left corner of page
doubleClick( x, y) - obvious
sendkeys( string ) - should allow an embedded enter key with `\r`
frameGrab( x1 ,y1 , x2, y1 ) - returns a pillow image


compareFrames( img1 , img2 , matchLevel ) - Well suited to Chrome Remote Desktop compression artifacts. 0<=MatchLevel<=1.0. 
alarm( string ) - displays string and makes a loud noise on the local computer until an operator acks it

# playwritescriptrecord

A python program for creating scripts. Cycles thru a menu like...

```
What should we do next?
1. Click
2. Double Click
3. Send Keys
4. Grab a frame
5. Compare screen to a grabbed frame and alert if different
6. End
```

At startup, it asks for a filename for the new script (adds .py) and then the debug URL (give a little hint how to get it from chrome).

For all actions except end, it should prompt the user for an optional comment that will go above the action in the script.

For the click and double click, it should probably grab a screenshot of the current full display and then present to the user on the local machine and ask them to pick where the action should happen.

For send keys, it should hint the available special keys (for now only enter)

For grab a frame it should ask for a name for the grabbed frame (valid python variable name that gets the pillow image).

For compare, it should present a list of defined grab names.

# UI

I think main interactions can be CLI, with a pop up graphics window for the mouse position capture?

