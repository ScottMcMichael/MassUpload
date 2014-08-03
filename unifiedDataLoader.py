#!/usr/bin/env python
# __BEGIN_LICENSE__
#  Copyright (c) 2009-2013, United States Government as represented by the
#  Administrator of the National Aeronautics and Space Administration. All
#  rights reserved.
#
#  The NGT platform is licensed under the Apache License, Version 2.0 (the
#  "License"); you may not use this file except in compliance with the
#  License. You may obtain a copy of the License at
#  http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
# __END_LICENSE__

import sys

from BeautifulSoup import BeautifulSoup

import os, glob, optparse, re, shutil, subprocess, string, time, urllib, urllib2

import multiprocessing, sqlite3

import mapsEngineUpload, IrgStringFunctions, IrgGeoFunctions

#------------------------------------------------------------------
# Global definitions

SENSOR_TYPE_HiRISE = 0
SENSOR_TYPE_HRSC   = 1
SENSOR_TYPE_CTX    = 2
SENSOR_TYPE_THEMIS = 3

STATUS_NONE      = 0
STATUS_UPLOADED  = 1
STATUS_CONFIRMED = 2

SENSOR_CODES = {'hirise' : SENSOR_TYPE_HiRISE,
                'hrsc'  : SENSOR_TYPE_HRSC,
                'ctx'   : SENSOR_TYPE_CTX,
                'THEMIS': SENSOR_TYPE_THEMIS}


#----------------------------------------------------------------

def man(option, opt, value, parser):
    print >>sys.stderr, parser.usage
    print >>sys.stderr, ''' Script for grabbing and uploading data files'''

    sys.exit()

class Usage(Exception):
    def __init__(self, msg):
        self.msg = msg

class TableRecord:
    '''Helper class for parsing table records'''
    
    # Variables
    data = None
    
    def __init__(self, row):
        self.data = row

    def tableId(self):
        return self.data[0]
    def sensor(self):
        return self.data[1]
    def subtype(self):
        return self.data[2]
    def setName(self):
        return self.data[3]
    def acqTime(self):
        return self.data[4]
    def status(self):
        return self.data[5]
    def version(self):
        return self.data[6]
    def remoteURL(self):
        return self.data[7]
    def assetID(self):
        return self.data[8]
    def uploadTime(self):
        return self.data[9]
    #def minLat(self): # TODO: Nice BB wrapper
    #    return self.data[10]

# Function that is passed in to findAllDataSets below.
def addDataRecord(db, sensor, subType, setName, remoteURL):
    '''Adds a sensor data entry in the database'''

    # Do nothing if this dataset is already in the database
    cursor = db.cursor()
    cursor.execute("SELECT * FROM Files WHERE sensor=? AND subtype=? AND setname=?",
                      (str(sensor), subType, setName))
    if cursor.fetchone() != None:
        return True

    print 'Adding ' + setName + ',  ' + subType
    
    cursor.execute("INSERT INTO Files VALUES(null, ?, ?, ?, null, 0, null, ?, null, null, null, null, null, null)",
               (str(sensor), subType, setName, remoteURL))
    db.commit()
    # TODO: Verify that the insertion went through?
    return True
    
#--------------------------------------------------------------------------------
# List of functions that need to be provided for each of the sensors!


#def getCreationTime(fileList):
#    """Extract the file creation time and return in YYYY-MM-DDTHH:MM:SSZ format"""
#    Takes the list of files returned by fetchAndPrepFile
#    return null

#def findAllDataSets(db, dataAddFunctionCall, sensorCode):
#    '''Add all known data sets to the SQL database'''
#    return False

#def fetchAndPrepFile(setName, subtype, remoteURL, workDir):
#    '''Retrieves a remote file and prepares it for upload'''
#    returns a list of created files with the first one being the one to upload


#--------------------------------------------------------------------------------
# Other functions

# TODO: Move to a helper file!
def getCurrentTimeString():
    """Return the current time in YYYY-MM-DDTHH:MM:SSZ format"""    
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def logWriter(logQueue, logPath):
    '''Listens for messages on the queue and writes them to a file.'''

    print 'Starting log writer'
    f = open(logPath, 'a') 
    while 1: # Run until the process is killed
        message = logQueue.get() # Wait for a new message
        if message == 'stop_queue': # Check for the quit signal
            break
        f.write(str(message))
        f.flush()
    f.close()
    print 'Writer stopped'


def uploadFile(dbPath, fileInfo, logQueue, workDir):
    """Uploads a remote file to maps engine"""
    
    print 'Uploading file ' + fileInfo.remoteURL()

    db = sqlite3.connect(dbPath)
    cursor = db.cursor()
    
    # Choose the "library" to use based on the sensor type
    # - The current version of each sensor's data files is set here.
    if   fileInfo.sensor() == SENSOR_TYPE_HiRISE:
        from hiriseDataLoader import fetchAndPrepFile, getCreationTime
        version = 1
    elif fileInfo.sensor() == SENSOR_TYPE_HRSC:
        from hrscDataLoader import fetchAndPrepFile, getCreationTime
        version = 1
    elif fileInfo.sensor() == SENSOR_TYPE_CTX:
        from ctxDataLoader import fetchAndPrepFile, getCreationTime
        version = 1
    else:
        raise Exception('Sensor type ' + fileInfo.sensor() + ' is not supported!')
    
    # Call sensor-specific function to fetch and prepare the file.
    localFileList = fetchAndPrepFile(fileInfo.setName(), fileInfo.subtype(), fileInfo.remoteURL(), workDir)
    if len(localFileList) == 0:
        raise Exception('Failed to retrieve any local files!')
    preppedFilePath = localFileList[0]
    
    if not os.path.exists(preppedFilePath):
        raise Exception('Prepped file does not exist: ' + preppedFilePath)

    # Extract a timestamp from the file
    timeString = getCreationTime(localFileList)
    cursor.execute("UPDATE Files SET acqTime=? WHERE idx=?", (timeString, str(fileInfo.tableId())))
    
    # Find out the bounding box of the file and generate a log string
    fileBbox = IrgGeoFunctions.getImageBoundingBox(preppedFilePath)
    bboxString = ('Bbox: ' + str(fileBbox[0]) +' '+ str(fileBbox[1]) +' '+ str(fileBbox[2]) +' '+ str(fileBbox[3]))
    cursor.execute("UPDATE Files SET minLon=?, maxLon=?, minLat=?, maxLat=? WHERE idx=?",
                   (str(fileBbox[0]), str(fileBbox[1]), str(fileBbox[2]), str(fileBbox[3]), str(fileInfo.tableId())))
    
    # Upload the file
    cmdArgs = [preppedFilePath, '--sensor', str(fileInfo.sensor()), '--acqTime', timeString]
    #print cmdArgs
    #assetId = mapsEngineUpload.main(cmdArgs)
    assetId = 12345 # DEBUG!
   
    #TODO: Check to make sure the file made it up!

    # Update the database
    currentTimeString = getCurrentTimeString()
    cursor.execute("UPDATE Files SET status=?, uploadTime=?, version=?, assetID=? WHERE idx=?",
                    (str(STATUS_UPLOADED), currentTimeString, str(version), str(assetId), str(fileInfo.tableId())))
    db.commit()
    db.close() 

    # Record that we uploaded the file
    logString = fileInfo.setName() +', '+ str(assetId) +', '+ bboxString + '\n' # Log path and the Maps Engine asset ID
    #print logString
    logQueue.put(logString)

        
    # Delete all the local files left by the prep function
    print 'rm ' + preppedFilePath
    #for f in localFileList:
        #os.remove(f)
    
    print 'Finished uploading data file!'
    return assetId


def uploadNextFile(dbPath, sensorCode, outputFolder, numFiles=1, numThreads=1):
    """Determines the next file to upload, uploads it, and logs it"""
    
    print 'Searching for next file to upload...'

    db = sqlite3.connect(dbPath)
    cursor = db.cursor()
        
    # query the SQL database for one or more entries for this sensor which have not been uploaded yet.
    cursor.execute('SELECT * FROM Files WHERE sensor=? AND status=? LIMIT ?',
                   (str(sensorCode), str(STATUS_NONE), str(numFiles)))
    rows = cursor.fetchall()
    db.close()
    if rows == []: # Make sure we found the next lines
        raise Exception('Could not find any data files left to upload!')
    
    # Create processing pool
    # Limit number of threads to numFiles+1
    if numThreads > numFiles+1:
        numThreads = numFiles+1
    print 'Spawning ' + str(numThreads) + ' worker threads'
    pool = multiprocessing.Pool(processes=numThreads+1) # One extra thread for the logWriter

    # Create multiprocessing manager and a queue
    manager = multiprocessing.Manager()
    queue   = manager.Queue()    

    # Start up the log writing thread
    logPath = os.path.join(outputFolder, 'activitylog.txt')
    print 'Writing output to file ' + logPath
    logResult = pool.apply_async(logWriter, args=(queue, logPath))
    
    # For each item we fetched, spawn a process to upload it.
    jobResults = []
    for line in rows:
        fileInfo = TableRecord(line) # Wrap the data line
        print 'Spawning thread for: '
        print str(fileInfo)
        jobResults.append(pool.apply_async(uploadFile, args=(dbPath, fileInfo, queue, outputFolder)))
    
    
    # Wait until all threads have finished
    print 'Waiting for all threads to complete...'
    for r in jobResults:
        r.get()
    
    # Stop the queue and all the threads
    print 'Cleaning up...'
    queue.put('stop_queue')
    pool.close()
    pool.join()
    
    print 'All threads finished!'
   
    return True
    
    
def checkUploads(db, sensorType):

    print 'Checking the status of uploaded files...'    

    cursor = db.cursor()

    # Get server authorization and hold on to the token
    bearerToken = mapsEngineUpload.authorize()

    MAX_NUM_RETRIES = 4  # Max number of times to retry (in case server is busy)
    SLEEP_TIME      = 1.1 # Time to wait between retries (Google handles only one operation/second)

    # Query the data base for all sensor data files which have been uploaded but not confirmed 
    cursor.execute('SELECT * FROM Files WHERE sensor=? AND status=?', (str(sensorType), str(STATUS_UPLOADED)))
    rows = cursor.fetchall()
    for row in rows:

        # Wrap the row to make it easier to get information
        o = TableRecord(row)
        
        # Check if this asset was uploaded
        print 'Checking asset ID = ' + o.assetID()
        for i in range(1,MAX_NUM_RETRIES):
            status, responseCode = mapsEngineUpload.checkIfFileIsLoaded(bearerToken, o.assetID())
            if (responseCode == 403) or (responseCode == 503):
                print 'Server is busy, sleeping ' + str(SLEEP_TIME) + ' seconds...'
                time.sleep(SLEEP_TIME)
            else:
                break
        
        if not status:
            print 'Data set ' + o.setName() + ' was not uploaded correctly!'
            # Update the file info in the database to show it was not updated correctly.
            # - TODO: Is there a way to make sure we re-upload to the same asset ID?
            cursor.execute("UPDATE Files SET status=? WHERE idx=?",
                           (str(STATUS_NONE), str(o.tableId())))
            db.commit()


    print 'Finished checking uploaded files.'
    
def getDataList(db, sensorCode):
    '''Update the list of available data sets from the given sensor'''

    # Choose the "library" to use based on the sensor type
    # - The current version of each sensor's data files is set here.
    if   sensorCode == SENSOR_TYPE_HiRISE:
        from hiriseDataLoader import findAllDataSets
    elif sensorCode == SENSOR_TYPE_HRSC:
        from hrscDataLoader import findAllDataSets
    elif sensorCode == SENSOR_TYPE_CTX:
        from ctxDataLoader import findAllDataSets
    else:
        raise Exception('Sensor type ' + sensorCode + ' is not supported!')
    
    return findAllDataSets(db, addDataRecord, sensorCode)

#--------------------------------------------------------------------------------

def main():

    print "Started unifiedDataLoader.py"

    # ----- Parse input arguments -----
    usage = "usage: unifiedDataLoader.py <sensor name> [--help][--manual]\n  "
    parser = optparse.OptionParser(usage=usage)
    
    parser.add_option("-u", "--upload", dest="upload", type=int,
                      help="Upload this many files instead of fetching the list.")

    parser.add_option("--checkUploads", action="store_true", default=False,
                                dest="checkUploads",  help="Verify that all uploaded files actually made it up.")

    parser.add_option("--threads", type="int", dest="numThreads", default=1,
                      help="Number of threads to use.")
                      
    parser.add_option("--manual", action="callback", callback=man,
                      help="Read the manual.")
    (options, args) = parser.parse_args()

    # Make sure the user passed in the name of the sensor
    try:
        options.sensorType = SENSOR_CODES[args[0].lower()]
    except:
        raise Exception('Did not recognize sensor name: ' + args[0])

    ## Now check for the output folder (working directory)
    #if len(args) != 2:
    #    raise Exception('Missing output folder!')
    #    return 1;
    #options.outputFolder = args[1]
    # The output path is hardcoded for now with subfolders for each sensor
    options.outputFolder = os.path.join('/home/smcmich1/data/google/', args[0].lower())
    # -- Done parsing input arguments --


    # Check the database connection
    # - Default should be to db = a thread-safe connection
    # - TODO: Find this database without hard coding it!
    dbPath = '/home/smcmich1/data/google/googleData.db'
    db = sqlite3.connect(dbPath)
    print 'Connected to database'
    
    print "Beginning processing....."
    
    startTime = time.time()
    
    # Make sure the working directory exists
    if not os.path.exists(options.outputFolder):
        os.mkdir(options.outputFolder)
    
    
    if options.checkUploads: # Check to see if uploaded files made it up ok
        checkUploads(db, options.sensorType)
        
    elif options.upload: # Upload one or more files
        db.close()
        uploadNextFile(dbPath, options.sensorType, options.outputFolder, options.upload, options.numThreads)
        
    else: # Update the database of data files
        getDataList(db, options.sensorType)
    
    
    endTime = time.time()
    
    print "Finished in " + str(endTime - startTime) + " seconds."
    return 0


if __name__ == "__main__":
    sys.exit(main())
