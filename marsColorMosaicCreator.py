
import os
import sys
import re
import subprocess
import numpy
import copy
import multiprocessing
import threading
import logging
import datetime
import time
import traceback
import shutil

import IrgGeoFunctions
import mosaicTileManager # TODO: Normalize caps!
import MosaicUtilities
import hrscImageManager
import hrscFileCacher

import stackImagePyramid

"""

Existing tools:
- RegisterHrsc.cpp
    - Input  = Basemap, HRSC
    - Output = spatialTransform
- writeHrscColorPairs.cpp
    - Input  = Basemap, HRSC, spatialTransform
    - Output = File containing pixel color pairs
- transformHrscImageColor.cpp
    - Input  = HRSC, colorTransform
    - Output = Color transformed HRSC image


Total Mars map width meters = 21338954.2
height = 10669477.2

Input basemap is 128x256 tiles, each tile 45x45, mpp = ~1852
- Total 32768 tiles.
- Between +/-60 degrees, 86x256 tiles = 22016 tiles (actually between +/-60.46875)
With 32x  increase, each tile is 1440x1440,   ~6MB,  190GB, mpp = ~58
With 64x  increase, each tile is 2880x2880,  ~24MB,  760GB, mpp = ~29
With 128x increase, each tile is 5760x5760,  ~95MB,    3TB, mpp = ~14.5 <-- Using this!
With 160x increase, each tile is 7200x7200, ~150MB,  4.6TB, mpp = ~11.6



If there are about 3600 HRSC images (more if we fetch updates from the last few months)
  at a batch size of 20, that is 180 batches!  To finish in two months (60 days) this 
  means 3 completed batches per day.

Batch procedure:
- Keep a count of how many images we have downloaded
- Keep processing until we have downloaded COUNT images
  - TODO: Run through the DB and add all images missing URL's to the bad image list.
- For each HRSC image, update all the tiles it overlaps.
- Before each tile is touched for the first time, make a backup of it!
- After a batch finishes, make the kml pyramid and send out an email.
- MANUAL INTERVENTION
  - Look at the kml pyramid and make sure things are ok.
  - Is there a way to highlight the modified tiles? 
- When a batch is confirmed, clean up the data and remove the backed up tiles.
- Start the next batch!

"""

#----------------------------------------------------------------------------
# Constants

NUM_DOWNLOAD_THREADS = 5 # There are five files we download per data set
NUM_PROCESS_THREADS  = 16

IMAGE_BATCH_SIZE = 2 # This should be set equal to the HRSC cache size


# Lunokhod 2
fullBasemapPath        = '/byss/smcmich1/data/hrscBasemap/projection_space_basemap.tif'
fullBasemapPath180     = '/byss/smcmich1/data/hrscBasemap180/projection_space_basemap180.tif'
outputTileFolder       = '/home/smcmich1/data/hrscNewOutputTiles'
backupFolder           = '/byss/smcmich1/data/hrscBasemap/output_tile_backups'
databasePath           = '/byss/smcmich1/data/google/googlePlanetary.db'
logFolder              = '/byss/smcmich1/data/hrscMosaicLogs'
hrscThumbnailFolder    = '/byss/smcmich1/data/hrscThumbnails'
hrscRegistrationFolder = '/byss/smcmich1/data/hrscRegistration'
BAD_HRSC_FILE_PATH     = '/byss/smcmich1/repo/MassUpload/badHrscSets.csv'
kmlPyramidFolder       = '/byss/docroot/smcmich1/hrscMosaicKml'
sourceHrscFolder       = '/home/smcmich1/data/hrscDownloadCache'
hrscOutputFolder       = '/home/smcmich1/data/hrscProcessedFiles'


# --- Folder notes ---
# - sourceHrscFolder holds the downloaded and preprocessed HRSC data
# - hrscOutputFolder holds the fully processed HRSC files
# - The current crop of tiles is written to outputTileFolder
# - The persistent set of final output tiles is kept in backupFolder



# Used to control the area we operate over
#HRSC_FETCH_ROI = None # Fetch ALL hrsc images
#HRSC_FETCH_ROI = MosaicUtilities.Rectangle(-180.0, 180.0, -60.0, 60.0) # No Poles
HRSC_FETCH_ROI = MosaicUtilities.Rectangle(   0.0,    180.0, -60.0, 60.0) # Right half: L2
#HRSC_FETCH_ROI = MosaicUtilities.Rectangle(-180.0, -0.0001, -60.0, 60.0) # Left half:  Alderaan

# DEBUG regions
#HRSC_FETCH_ROI = MosaicUtilities.Rectangle(-116.0, -110.0, -2.0, 3.5) # Restrict to a mountain region
#HRSC_FETCH_ROI = MosaicUtilities.Rectangle(133.0, 142.0, 46, 50.0) # Viking 2 lander region
#HRSC_FETCH_ROI = MosaicUtilities.Rectangle(-78.0, -63.0, -13.0, -2.5) # Candor Chasma region
#HRSC_FETCH_ROI = MosaicUtilities.Rectangle(-161.0, -154.0, -60.0, -50.0) # Region near -60 lat
#HRSC_FETCH_ROI = MosaicUtilities.Rectangle(62.0, 67.0, -35.0, -28.0) # Coronae Scolpulus
#HRSC_FETCH_ROI = MosaicUtilities.Rectangle(177, 183, 11.0, 17.0) # Orcus Patera on dateline


#================================================================================



# Set up the log path here.
# - Log tiles are timestamped as is each line in the log file
LOG_FORMAT_STR = '%(asctime)s %(name)s %(message)s'
currentTime = datetime.datetime.now()
logPath = os.path.join(logFolder, ('hrscMosaicLog_%s.txt' % currentTime.isoformat()) )
logging.basicConfig(filename=logPath,
                    format=LOG_FORMAT_STR,
                    level=logging.DEBUG)

#-----------------------------------------------------------------------------------------
# Functions

def getDiskUsage():
    '''Return simple disk space usage information'''
    cmd = ['df', '-h']
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    textOutput, err = p.communicate()
    return textOutput


def cacheManagerThreadFunction(databasePath, hrscDownloadFolder, hrscProcessedFolder, inputQueue, outputQueue):
    '''Thread to allow downloading of HRSC data in parallel with image processing.
       The input queue recieves three types of commands:
           "STOP" --> Finish current tasks, then exit.
           "KILL" --> Immediately kill all the threads and exit.
           "FETCH data_set_name" --> Fetch the specified data set
           "FINISHED data_set_name" --> Signals that this data set can be safely deleted
'''

    logger = logging.getLogger('DownloadThread')

    # Echo logging to stdout
    echo = logging.StreamHandler(sys.stdout)
    echo.setLevel(logging.DEBUG)
    echo.setFormatter(logging.Formatter(LOG_FORMAT_STR))
    logger.addHandler(echo)



    # Initialize a process pool to be managed by this thread
    downloadPool = None
    if NUM_DOWNLOAD_THREADS > 1:
        downloadPool = multiprocessing.Pool(processes=NUM_DOWNLOAD_THREADS)

    # Set up the HRSC file manager object
    logger.info('Initializing HRSC file caching object')
    hrscFileFetcher = hrscFileCacher.HrscFileCacher(databasePath, hrscDownloadFolder, hrscProcessedFolder,
                                                    BAD_HRSC_FILE_PATH, downloadPool)

    while True:

        # Fetch the next requested data set from the input queue
        request = inputQueue.get() 

        # Handle stop request
        if request == 'STOP':
            logger.info('Download thread manager received stop request, stopping download threads...')
            # Gracefully wait for current work to finish
            if downloadPool:
                downloadPool.close()
                downloadPool.join()  
            break
            
        if request == 'KILL':
            logger.info('Download thread manager received kill request, killing download threads...')
            # Immediately stop all work
            if downloadPool:
                downloadPool.terminate()
                downloadPool.join()  
            break
      
        if 'FETCH' in request:
            dataSet = request[len('FETCH'):].strip()
            logger.info('Got request to fetch data set ' + dataSet)
            # Download this HRSC image using the thread pool
            try:
                hrscInfoDict = hrscFileFetcher.fetchHrscDataSet(dataSet)
            except Exception, e:
                # When we fail to fetch a data set, send out a failure message and keep going.
                logger.error('Caught exception fetching data set ' + dataSet + '\n' + 
                             str(e) + '\n' + str(sys.exc_info()[0]) + '\n')
                logger.error(sys.exc_info()[0])
                print(traceback.format_exc())
                hrscInfoDict = {'setName': dataSet, 'error': True}
                outputQueue.put(hrscInfoDict)
                continue    
            logger.info('Finished fetching data set ' + dataSet)
            # Put the output information on the output queue
            outputQueue.put(hrscInfoDict)

   
        
        #--> Need to make sure we never delete an image until we are finished using it

        # TODO: Implement 'finished' message?

    # We only get here when we break out of the main loop
    outputQueue.put('STOPPED')
    logger.info('Download manager thread stopped.')



def getCoveredOutputTiles(basemapInstance, hrscInstance):
    '''Return a bounding box containing all the output tiles covered by the HRSC image'''
    
    # This bounding box can be in either the +/-180 range or the 0-360 range
    hrscBoundingBoxDegrees = hrscInstance.getBoundingBoxDegrees()
    #print 'HRSC BB = ' + str(hrscBoundingBoxDegrees)
    
    # Expand the computed bounding box a little bit to insure that
    #  we don't miss any tiles around the edges.
    BUFFER_SIZE = 0.1 # BB buffer size in degrees
    hrscBoundingBoxDegrees.expand(BUFFER_SIZE, BUFFER_SIZE)
        
    # DEBUG!  Restrict to a selected area.
    hrscBoundingBoxDegrees = HRSC_FETCH_ROI.getIntersection(hrscBoundingBoxDegrees)

    intersectTileList = basemapInstance.getIntersectingTiles(hrscBoundingBoxDegrees)
    return intersectTileList
    

def updateTileWithHrscImage(hrscTileInfoDict, outputTilePath, tileLogPath):
    '''Update a single output tile with the given HRSC image'''

    # Append all the tiles into one big command line call
    hrscTiles = ''
    cmd = './hrscMosaic ' + outputTilePath +' '+ outputTilePath
    for hrscTile in hrscTileInfoDict.itervalues():    
        #try:
        # This pastes the HRSC tile on top of the current output tile.  Another function
        #  will have made sure the correct output tile is in place.
        cmd += (' '+ hrscTile['newColorPath'] +' '+
                  hrscTile['tileMaskPath'] +' '+ hrscTile['tileToTileTransformPath'])
        hrscTiles += hrscTile['prefix'] + ', '

    # Execute the command line call
    MosaicUtilities.cmdRunner(cmd, outputTilePath, True)

    # Return the path to log the success to
    return (tileLogPath, hrscTiles)
    
    


def updateTilesContainingHrscImage(basemapInstance, hrscInstance, pool=None):
    '''Updates all output tiles containing this HRSC image'''

    logger = logging.getLogger('MainProgram')

    # Find all the output tiles that intersect with this
    outputTilesList = getCoveredOutputTiles(basemapInstance, hrscInstance)

    hrscSetName = hrscInstance.getSetName()
    mainLogPath = basemapInstance.getMainLogPath()
    
    # Skip this function if we have completed adding this HRSC image
    if basemapInstance.checkLog(mainLogPath, hrscSetName):
        logger.info('Have already completed adding HRSC image ' + hrscSetName + ',  skipping it.')
        basemapInstance.updateLog(mainLogPath, hrscSetName) # This should have already been logged!
        return

    logger.info('Started updating tiles for HRSC image ' + hrscSetName)
    #logger.info('Found overlapping output tiles:  ' + str(outputTilesList))
    
    # Do all the basemap calls first using the pool before doing the HRSC work
    # - This will make sure that the proper file exists in the output directory to 
    #   paste incoming HRSC tiles on top of.
    logger.info('Making sure we have required basemap tiles for HRSC image ' + hrscSetName)
    basemapInstance.generateMultipleTileImages(outputTilesList, pool, force=False)
    
    if pool:
        logger.info('Initializing tile output tasks...')
    
    # Loop through all the tiles
    tileResults = []
    for tileIndex in outputTilesList:

        tileBounds = basemapInstance.getTileRectDegree(tileIndex)
                
        # Retrieve the needed paths for this tile
        (smallTilePath, largeTilePath, grayTilePath, outputTilePath, tileLogPath, tileBackupPath) = \
            basemapInstance.getPathsForTile(tileIndex) 
    
        # Have we already written this HRSC image to this tile?
        comboAlreadyWritten = basemapInstance.checkLog(tileLogPath, hrscSetName)
        if comboAlreadyWritten: #Don't want to double-write the same image.
            logger.info('-- Skipping already written tile: ' + str(tileIndex)) 
            continue

        logger.info('Using HRSC image ' + hrscSetName + ' to update tile: ' + str(tileIndex))
        logger.info('--> Tile bounds = ' + str(tileBounds))
        #logger.info('\nMaking sure basemap info is present...')
    
        # Get information about which HRSC tiles to paste on to the basemap
        hrscTileInfoDict = hrscInstance.getTileInfo(basemapInstance, tileBounds, tileIndex.getPostfix())
        if not hrscTileInfoDict: # If there are no HRSC tiles to use, move on to the next output tile!
            continue
    
        # Update the selected tile with the HRSC image
        if pool:
            # Send the function and arguments to the thread pool
            dictCopy = copy.copy(hrscTileInfoDict)
            tileResults.append(pool.apply_async(updateTileWithHrscImage,
                                                args=(dictCopy, outputTilePath, tileLogPath)))
        else: # Just run the function
            updateTileWithHrscImage(hrscTileInfoDict, outputTilePath, tileLogPath)
        
        #print 'DEBUG - only updating one tile!'
        #break

    if pool: # Wait for all the tasks to complete
        logger.info('Finished initializing tile output tasks.')
        logger.info('Waiting for tile processes to complete...')
        for result in tileResults:
            # Each task finishes by returning the log path for that tile.
            # - Record that we have used this HRSC/tile combination.
            # - This requires that tiles with no HRSC tiles do not get assigned a task.
            (tileLogPath, hrscTilePrefixList) = result.get()
            basemapInstance.updateLog(tileLogPath, hrscSetName, hrscTilePrefixList)
            
            
        logger.info('All tile writing processes have completed')
        
    # Log the fact that we have finished adding this HRSC image
    print 'Log path: #' + mainLogPath+ '#'
    print 'Set name: #' + hrscSetName+ '#'
    basemapInstance.updateLog(mainLogPath, hrscSetName)

    logger.info('Finished updating tiles for HRSC image ' + hrscSetName)


def generateAllUpsampledBasemapTiles(basemapInstance, pool):
    '''Generate all the basemap tiles from the input low-res image.
       There are 128*256 = 32,768 tiles in the full image, about 20,000 tiles
       in the +/-60 version.'''

    print 'GENERATING ALL BASEMAP TILES'

    # Get all the tiles we are interested in.
    # Should have tiles from -180 to 180 in the +/-60 range. (on /byss)
    allTileList = basemapInstance.getIntersectingTiles(MosaicUtilities.Rectangle(-180, 180, -60, 60))
    
    # Make all the outputs.  This will take a while!
    basemapInstance.generateMultipleTileImages(allTileList, pool, force=False)
    

#================================================================================

print 'Starting basemap enhancement script...'

startTime = time.time()

logger = logging.getLogger('MainProgram')

# Echo logging to stdout
echo = logging.StreamHandler(sys.stdout)
echo.setLevel(logging.DEBUG)
echo.setFormatter(logging.Formatter(LOG_FORMAT_STR))
logger.addHandler(echo)


# Initialize the multi-threading worker pool
processPool  = None
if NUM_PROCESS_THREADS > 1:
    processPool = multiprocessing.Pool(processes=NUM_PROCESS_THREADS)

logger.info('==== Initializing the base map object ====')
basemapInstance = mosaicTileManager.MarsBasemap(fullBasemapPath, outputTileFolder, backupFolder)
basemapInstance.copySupportFilesFromBackupDir() # Copies the main log from the backup dir to output dir
basemapInputsUsedLog = basemapInstance.getMainLogPath()
print 'CHECKING LOG PATH: ' + basemapInputsUsedLog

# Create another basemap instance centered on 180 lon.
# - This instance should not be creating any tiles in its output folders!
# - This instance is only for registration and preprocessing of wraparound HRSC images.
dummyFolder = '/dev/null'
basemapInstance180 = mosaicTileManager.MarsBasemap(fullBasemapPath180, dummyFolder, dummyFolder, center180=True)
logger.info('--- Finished initializing the base map object ---\n')

## Run once code to generate all of the starting basemap tiles!
#generateAllUpsampledBasemapTiles(basemapInstance, processPool)
#raise Exception('DONE GENERATING ALL INPUT TILES')

# Get a list of the HRSC images we are testing with
tempFileFinder = hrscFileCacher.HrscFileCacher(databasePath, sourceHrscFolder, 
                                               hrscOutputFolder, BAD_HRSC_FILE_PATH)

# Run-once code to find all the incomplete data sets in one pass
#fullImageList = tempFileFinder.getHrscSetList()
#tempFileFinder.findIncompleteSets(fullImageList)
#raise Exception('DONE FINDING BAD SETS')


fullImageList = tempFileFinder.getHrscSetList(HRSC_FETCH_ROI)
tempgFileFinder = None # Delete this temporary object

logger.info('Identified ' + str(len(fullImageList)) + ' HRSC images in the requested region.')


# Prune out all the HRSC images that we have already added to the mosaic.
hrscImageList = []
for hrscSetName in fullImageList:
    if basemapInstance.checkLog(basemapInputsUsedLog, hrscSetName):
        logger.info('Have already completed adding HRSC image ' + hrscSetName + ',  skipping it.')
    else:
        hrscImageList.append(hrscSetName)
#hrscImageList = ['h8289_0000'] # DEBUG

numDataSetsRemainingToProcess = len(hrscImageList)
logger.info('Num data sets remaining to process = ' + str(numDataSetsRemainingToProcess))

# Restrict the image list to the batch size
# - It would be more accurate to only count valid images but this is good enough
hrscImageList = hrscImageList[0:IMAGE_BATCH_SIZE]
try:
  batchName = hrscImageList[0]
except:
  batchName = 'Default Name'
logger.info('Image list for this batch: ' + str(hrscImageList))

if len(hrscImageList) > 0:
  # Set up the HRSC file manager thread
  logger.info('Starting communication queues')
  downloadCommandQueue  = multiprocessing.Queue()
  downloadResponseQueue = multiprocessing.Queue()
  logger.info('Initializing HRSC file caching thread')
  downloadThread = threading.Thread(target=cacheManagerThreadFunction,
                                    args  =(databasePath, sourceHrscFolder, hrscOutputFolder,       
                                            downloadCommandQueue, downloadResponseQueue)
                                   )
  downloadThread.daemon = True # Needed for ctrl-c to work
  logger.info('Running thread...')
  downloadThread.start()


  # Go ahead and send a request to fetch the first HRSC image
  logger.info('Sending FETCH command: ' + hrscImageList[0])
  downloadCommandQueue.put('FETCH ' + hrscImageList[0])


# Loop through input HRSC images
numHrscDataSets   = len(hrscImageList) 
processedDataSets = []
failedDataSets    = []
setProcessTimes   = []
for i in range(0,numHrscDataSets): 
    
    # Get the name of this and the next data set
    hrscSetName = hrscImageList[i]
    nextSetName = None
    if i < numHrscDataSets-1:
        nextSetName = hrscImageList[i+1]
        # Go ahead and submit the fetch request for the next set name.
        logger.info('Sending FETCH command: ' + nextSetName)
        downloadCommandQueue.put('FETCH ' + nextSetName)

    # Notes on downloading:
    # - Each iteration of this loop commands one download, and waits for one download.
    # - The queues keep things in order, and the download thread handles one data set at a time.
    # - This means that we can have one data set downloading while one data set is being processed.
    # - The next improvement to be made would be to download multiple data sets at the same time.

   
    # Pick a location to store the data for this HRSC image
    thisHrscFolder = os.path.join(hrscOutputFolder, hrscSetName)

    try:

        logger.info('=== Fetching HRSC image ' + hrscSetName + ' ===')

        # Fetch the HRSC data from the web
        hrscFileInfoDict = downloadResponseQueue.get() # Wait for the parallel thread to provide the data
        if not 'setName' in hrscFileInfoDict:
            raise Exception('Ran out of HRSC files, processing stopped!!!')
        if hrscFileInfoDict['setName'] != hrscSetName:
            raise Exception('Set fetch mismatch!  Expected %s, got %s instead!' % 
                             (hrscSetName, hrscFileInfoDict['setName']))
        logger.info('Received fetch information for ' + hrscSetName)

        if 'error' in hrscFileInfoDict:
            logger.info('Skipping data set ' + hrscSetName + ' which could not be fetched.')
            continue

        #raise Exception('STOPPED AFTER DOWNLOAD')

        setStartTime = time.time()
        logger.info('\n=== Initializing HRSC image ' + hrscSetName + ' ===')

        # Preprocess the HRSC image
        hrscInstance = hrscImageManager.HrscImage(hrscFileInfoDict, thisHrscFolder,
                                                  basemapInstance, basemapInstance180,
                                                  False, processPool)

        logger.info('--- Now initializing high res HRSC content ---')


        # Complete the high resolution components
        hrscInstance.prepHighResolutionProducts()
        
        logger.info('--- Finished initializing HRSC image ---\n')

        # Call the function to update all the output images for this HRSC image
        updateTilesContainingHrscImage(basemapInstance, hrscInstance, processPool)

        logger.info('<<<<< Finished writing all tiles for this HRSC image! >>>>>')
        
        # Record that we finished processing this HRSC image
        processedDataSets.append( (hrscSetName, hrscInstance.getBoundingBoxDegrees()) )

        # Record how long this data set took to process
        setStopTime = time.time()
        setProcessTimes.append(setStopTime - setStartTime)

    except Exception, e:
        # When we fail to fetch a data set, log a failure message and keep going.
        failedDataSets.append(hrscSetName)
        logger.error('Caught exception processing data set ' + hrscSetName + '\n' + 
                     str(e) + '\n' + str(sys.exc_info()[0]) + '\n')
        logger.error(traceback.format_exc())
            

numHrscImagesProcessed = len(processedDataSets)

PROCESS_POOL_KILL_TIMEOUT = 5 # The pool should not be doing any work at this point!
if processPool:
    logger.info('Cleaning up the processing thread pool...')
    # Give the pool processes a little time to stop, them kill them.
    processPool.close()
    time.sleep(PROCESS_POOL_KILL_TIMEOUT)
    processPool.terminate()
    processPool.join()

try:
    downloadCommandQueue.put('STOP') # Stop the download thread
    downloadThread.join()
except:
    print 'Exception thrown shutting down the downloader!'

#raise Exception('DEBUG - SKIP HTML GENERATION!!')

# Copy some debug files to a centralized location for easy viewing
# TODO: The HRSC manager should take care of this?
for dataSet in processedDataSets:

    try:
        setName = dataSet[0]
        # Copy the low res nadir image overlaid on a section of the low res mosaic
        debugImageInputPath = os.path.join(hrscOutputFolder, setName+'/'+setName+'_registration_debug_mosaic.tif')
        debugImageCopyPath  = os.path.join(hrscRegistrationFolder, setName+'_registration_image.tif')
        shutil.copy(debugImageInputPath, debugImageCopyPath)

        # Copy the low res nadir image to a thumbnail folder
        debugImageInputPath = os.path.join(hrscOutputFolder, setName+'/'+setName+'_nd3_basemap_res.tif')
        debugImageCopyPath  = os.path.join(hrscThumbnailFolder, setName+'_nadir_thumbnail.tif')
        shutil.copy(debugImageInputPath, debugImageCopyPath)
    except:
        logger.error('Error copying a debug file for set ' + setName)


# Compute the run time for the output message
SECONDS_TO_HOURS = 1.0 / (60.0*60.0)
stopTime = time.time()
runTime  = (stopTime - startTime) * SECONDS_TO_HOURS

if (numHrscImagesProcessed > 0) or (IMAGE_BATCH_SIZE == 0):
    # Generate a KML pyramid of the tiles for diagnostics
    kmlPyramidLocalPath  = stackImagePyramid.main(outputTileFolder, kmlPyramidFolder, processedDataSets)
    pos                  = kmlPyramidLocalPath.find('/smcmich1')
    kmlPyramidWebAddress = 'http://byss.arc.nasa.gov' + kmlPyramidLocalPath[pos:]

    # Send a message notifiying that the output needs to be reviewed!
    msgText = '''
Finished processing ''' +str(numHrscImagesProcessed) + ''' HRSC images!

elapsed time = ''' + str(runTime) + ''' hours.

Number remaining data sets = ''' + str(numDataSetsRemainingToProcess) + '''

KML pyramid link:
'''+kmlPyramidWebAddress+'''

Registration debug images are here:
'''+hrscRegistrationFolder+'''
--> To clear:
rm '''+hrscRegistrationFolder+'''/*.tif

Image thumbnails are stored here:
'''+hrscThumbnailFolder+'''
Don't clear these!
-------

To undo the tile changes:
rm  '''+outputTileFolder+'''/*

To accept the tile changes:
rsync  --update --existing -avz '''+outputTileFolder +'/ '+ backupFolder+'''/
rm  '''+outputTileFolder+'''/*

To start the next batch, run:
/byss/smcmich1/run_hrsc_basemap_script.sh

Disk usage info:
''' + getDiskUsage()+'''
Processed image list:
'''
    index = 0
    for i in processedDataSets:
        msgText += i[0] + ' in ' + str(setProcessTimes[index]/60.0)+ ' minutes\n'
        index += 1
    if failedDataSets:
      msgText += '\n Failed image list:\n'
      for i in failedDataSets:
          msgText += i + '\n'
else:
    msgText = '''ERROR: No HRSC images in the batch could be processed!\n''' + str(failedDataSets)
    
    
MosaicUtilities.sendEmail('scott.t.mcmichael@nasa.gov', 
                          'HRSC map batch '+batchName+' completed',
                          msgText)

logger.info('Basemap generation script completed!')


# Commands for generating the 180 centered image
# gdal_translate projection_space_basemap.tif left.tif -srcwin 0 0 5760 5760
# gdal_translate projection_space_basemap.tif right.tif -srcwin 5760 0 5760 5760
# montage -mode Concatenate -tile 2x1 -background black  -depth 8  right.tif left.tif center180.tif
#gdal_translate center180.tif projection_space_basemap180.tif -a_srs "+proj=eqc +lon_0=180 +lat_ts=0 +lat_0=0 +a=3396200 +b=3376200 units=m" -a_ullr -10669477.100 5334738.600 10669477.100 -5334738.600



