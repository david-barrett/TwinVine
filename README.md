**TwinVine**


![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeederA.png)


TwinVine is the home of TWO packages  [Vinefeeder](https://github.com/vinefeeder/TwinVine/blob/main/packages/vinefeeder/src/vinefeeder/README.md)
and [Envied](https://github.com/vinefeeder/TwinVine/blob/main/packages/envied/README.md)

TwinVine is the easy way to handle your download tasks. 
* When you have an exact program url - just use envied as a command line call.
* When you only have the program name - just start with a search in vinefeeder.
* When you dont know what you want; use the browse function.
* Browse media categories like Film, Drama or Sport, for a selected service.
* Batch Mode: select multiple downloads from various services and download all together.
 


**usage**

TwinVine is a sophisticated piece of software engineering, (if I am allowed to say that) , handling two Python packages at once, each inter-playing with the other. It needs treating differently from anything you will have used before. 

All access must be done via the package manager - uv. You have two ways in

```
    uv run vinefeeder
    uv run envied dl --select-titles <service> <url>	
	

	To go to the command line use of 'envied', on Linix you may select 'run envied' after clicking the GUI envied button. On Windows, close the GUI directly, or ctrl+c to end the program and return to the terminal.
```

	
**Installation** - with binaries and python already installed

uv is the package manager and loads both VineFeeder and Envied together.  Envied runs independenly or may be called by Vinefeeder.

If you do not alrealy have uv as a python package try to install it first, using pip -  

```
pip install uv
or
python3 -m pip install uv
or use your system's package manager to install python-uv
```

Install TwinVine; 

the following installs the latest version directly from the GitHub repository:

```shell
git clone https://github.com/vinefeeder/TwinVine.git
cd TwinVine
uv clean
uv lock
uv sync
uv run vinefeeder --help or
uv run envied --help
```

**Installation for Windows** with a bare machine and novice user. (Why not print out these instructions so you can tick them off as you work through them?)
	
You are going to install all the required binary files and automatically add then to system variable - Path.  The Python interpreter will be installed automatically too.


	- download git from https://github.com/git-for-windows/git/releases/download/v2.52.0.windows.1/Git-2.52.0-64-bit.exe  and run the installer
	- re-start your machine
	- Open Start
	- Type PowerShell and select open PowerShell
	- Within PowerShell change directory, chdir, or cd, to your chosen location, where TwinVine is to be installed, and type the following command followed by enter,
	
		
```
git clone https://github.com/vinefeeder/TwinVine.git
```

	
	- Files will be downloaded, a folder called TwinVine will be created.
	- Close PowerShell and re-open with admininstrator privileges. Do...
	- Open Start
	- Type PowerShell
	- Right-click Windows PowerShell → Run as administrator
	- Inside PowerShell, change directory to TwinVine (cd TwinVine) and run the following command by copying or typing the line, followed by pressing enter.
	
	
```
 powershell -ExecutionPolicy Bypass -File .\Install-media-tools.ps1
```

	
	- Watch the installation, a number of binary files will be downloaded and installed to C:\Tool\bin. Installation will take a while. After finishing, close PowerShell and restart your machine.
	- Open Start
	- Type PowerShell
	- Type uv [return]. Expect to see a screen of help.  If uv did not install from the Install-media-tools.ps1 script you will not see any response. Uv is a python package manager.
	- If uv is not installed close PowerShell and re-open as administrator, so 
	- Open Start
	- Type PowerShell
	- Right-click Windows PowerShell → Run as administrator	
	- Type powershell -ExecutionPolicy Bypass -c "irm https://github.com/astral-sh/uv/releases/download/0.9.18/uv-installer.ps1 | iex" [return]

	- Close PowerShell and re-start your machine.
	- Type [WindowsKey]+R to open PowerShell, 
	- cd to TwinVine and type each line below followed by return. Some commands will take a while to finish.
	  	
```
	uv lock
	uv sync  
	cp .\packages\envied\src\envied\envied-working-example.yaml .\packages\envied\src\envied\envied.yaml
	uv run vinefeeder --help
	uv run envied --help
	uv run envied dl -?
	
```
That's it for Windows; uv run vinefeeder OR uv run envied dl -? to get started!  
Vinefeeder offers a graphical interace for some services; envied is a command-line tool for all services offered.


**Installation for Linux** with a bare machine.

There is an installation file to install binaries. Install-media-tools.sh. Open it in a text editor and edit lines 6 and 7. Change the Debian/Ubuntu package-manager command 'apt-get' to whatever your package manager uses (dnf, pacman, yast, etc)  
Then save and close and run the script with 
```
sudo bash ./Install-media-tools.sh
```
The script will take some time to install. Check uv is installed.
If not, you can install uv with the following command in a terminal window

```
wget -qO- https://astral.sh/uv/install.sh | sh
```
Finally, cd to TwinVine and run each command in order,

```
	uv lock
	uv sync  
	cp ./packages/envied/src/envied/envied-working-example.yaml ./packages/envied/src/envied/envied.yaml
	uv run vinefeeder --help
	uv run envied --help
	uv run envied dl -?
	
```

  
That's it for Linux; uv run vinefeeder OR uv run envied dl -? to get started!  
Vinefeeder offers a graphical interace for some services; envied is a command-line tool for all services offered.


**Locations**

As configured your files will be downloaded to TwinVine/packages/envied/src/downloads/  
The envied.yaml can be edited for the download location - use a full path, on  windows with forward slashes C:/Users/Downloads and Linux /home/user/Downloads, for example.
Cookies: As configured the Cookies folder is in packages/envied/src/. Each cookie file - type .txt should be names exactly as the service eg DNSP.txt and you personal login cookie placed inside.
WVDs: A CDM - called device.wvd is located at TwinVine/WVDs 
Vaults:  No local vaults are configured but a remote vault is used for caching and fetching. Often times you will see DRMLab s the source of the license key -saving unnecessary requests to a license server.

**Linux**

Linux systems are known to screen freeze after envied has finished a download.
The top level vinefeeder config file at  TwinVine/packages/vinefeeder/src/vinefeeder/config.yaml should have   TERMINAL_RESET: True   set.

**Services**

Vinefeeder currently has 10 services for which search, browse and list-select are available  
  
  ALL4  BBC  ITVX  MY5 PLEX RTE STV  TPTV  TVNZ  U 
  
Envied has   

ALL4  AUBC  CBS CWTV DSCP  iP   MAX   MY5   NF   PCOK PLEX RTE  ROKU  SPOT  TPTV  TVNZ  YTBE
ARD   CBC   CTV  DSNP  ITV  MTSP  NBLA  NRK  PLUTO  RTE   STV   TUBI  UKTV  ZDF
These services have web-origins and not all have been tested by me.  
  
**Other README's""
    TwinVine/packages/vinefeeder/src/vinefeeder/README.md  
    for details for confuring Envied download options on a service by service basis.
    TwinVine/packages/envied/README.md  links to wiki (unshackle - envied's parent)



Images
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeederA1.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder1.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder2.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder4.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder5.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder6.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder7.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder8.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder9.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder10.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder11.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeederB.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/hellyes.png)
    



