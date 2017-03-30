# TODO: Check correctness of this implementation, might contain some basepair shifts due to 0vs1 based genome stuff

import m2p
import prep
from Bio.Seq import Seq
import numpy as np
import pandas as pd

import re
import pysam
import parse
import collections

import bisect as bs

fastaIdFormat='>Pr.Id:{};Pr.Wi:{};Pr.Wn:{}\n{}\n'
#referenceNameFormat='>RD:{};IN:{}'


def loadIni(iniFile):
	# Read data from file, stuff it into a dict
	settings=dict()
	with open(iniFile,'r') as iniFile:
		for line in iniFile:
			splitLine = line.split()
			settings[splitLine[0]] = [x for x in splitLine[1:] if x != '']

	# Integer lists
	for key in ['prm_start','prm_end','vp_start','vp_end','win_start','win_end']:
		settings[key]=[int(x) for x in settings[key]]

	# Temporary solution to error in naming
	settings['prm_seq'] = settings['pr_seq']

	# Check lists that should be of equal length
	linked=[
			['prm_seq','prm_start','prm_end'],
			['re_name','re_seq'],
			['win_start','win_end'],
			['vp_name','vp_chr','vp_start','vp_end']
		]
	for listed in linked:
		assert len(set([len(settings[x]) for x in listed])) == 1, 'Error: different lengths for linked data:'+','.join(str(x) for x in listed)

	return settings


### makeprimerfa implementation ###

def splitStringTo(item,maxLen=50):
	return [str(item[ind:ind+maxLen]) for ind in range(0, len(item), maxLen/2)]


# In case of leftover sequence the last bit is attached to the preceding chunk
def seqToFasta(sequence,baseId):
	targetLen = 50
	split = splitStringTo(sequence,maxLen=targetLen)
	if len(split) > 1:
		while len(split[0]) != len(split[-1]):
			popper = split.pop(-1)
		split[-1] = split[-1][:targetLen/2]+popper
	outString = ''
	for i,val in enumerate(split):
		outString+=fastaIdFormat.format(
			baseId, i+1, len(split),
			val)

	return outString


# TODO: Improve error message on faulty primes sequences
def getPrimerSeqs(dataInfo):
	primerSeqs = []
	for i, val in enumerate(dataInfo['prm_seq']):
		leftSeq = prep.getFastaSequence(
				dataInfo['genome_build'][0],
				dataInfo['vp_chr'][0],
				dataInfo['prm_start'][i]-300,
				dataInfo['prm_end'][i]).upper()
		leftIndex = leftSeq.rfind(dataInfo['re_seq'][0])
		leftPrimerSeq = Seq(leftSeq[leftIndex:]).reverse_complement()

		rightSeq = prep.getFastaSequence(
				dataInfo['genome_build'][0],
				dataInfo['vp_chr'][0],
				dataInfo['prm_start'][i]-1,
				dataInfo['prm_end'][i]+300).upper()
		rightIndex = rightSeq.find(dataInfo['re_seq'][0]) + len(dataInfo['re_seq'][0]) - 1
		rightPrimerSeq = rightSeq[:rightIndex]

		assert max(leftPrimerSeq.find(dataInfo['prm_seq'][i]),
			rightPrimerSeq.find(dataInfo['prm_seq'][i])) >= 0, 'Primer sequence is wrong\n'+str(dataInfo['prm_seq'][i])
		assert min(leftPrimerSeq.find(dataInfo['prm_seq'][i]),
			rightPrimerSeq.find(dataInfo['prm_seq'][i])) <= 0, 'Primer sequence is ambigious\n'+str(dataInfo['prm_seq'][i])

		if rightPrimerSeq.find(dataInfo['prm_seq'][i]) >= 0:
			primerSeqs.append(rightPrimerSeq)
		else:
			primerSeqs.append(leftPrimerSeq)

	return primerSeqs


def writePrimerFasta(primerSeqs,targetFile):
	with open(targetFile,'w') as outFasta:
		for i,seq in enumerate(primerSeqs):
			outFasta.write(seqToFasta(seq,str(i+1)))


### cleavereads implementation ###

# This class can perhaps be replaced by a panda frame later
class SimpleRead(object):
	#fastaIdFormat='PR{}_{}-{}'#s06.fastaIdFormat
	def __init__(self, read, prmLen):
		primerDict = dict(item.split(":") for item in read.query_name.split(";"))

		self.prmType = int(primerDict['Pr.Id'][-1])
		self.prmFlag = read.flag & ~(1<<8) # Set 9th bit to 0 to state primary alignment
		self.readID = int(dict(item.split(":") for item in read.reference_name.split(";"))['Rd.Id'])
		self.startAln = read.reference_start
		self.endAln = read.reference_start + read.infer_query_length(always=True)
		self.prmSize = prmLen[int(self.prmType)-1] # Should be length of the original primer?
						# prm_len{prm_info(ai,1)}(prm_info(ai,8))
		self.alnErr = [sum(x[1] for x in read.cigartuples if x[0] in [1,2])]
		self.winInd = [int(primerDict['Pr.Wi'])]
		self.winNum = int(primerDict['Pr.Wn'])
		self.cigar = [read.cigarstring]

		# Cigar string information
		# M 	BAM_CMATCH 		0
		# I 	BAM_CINS 		1
		# D 	BAM_CDEL 		2
		# N 	BAM_CREF_SKIP 	3
		# S 	BAM_CSOFT_CLIP 	4
		# H 	BAM_CHARD_CLIP 	5
		# P 	BAM_CPAD 		6
		# = 	BAM_CEQUAL 		7
		# X 	BAM_CDIFF 		8

	def toList(self):
		return [
			self.prmType,
			self.prmFlag,
			self.readID,
			self.startAln,
			self.endAln,
			self.prmSize,
			self.alnErr,
			self.winInd,
			self.winNum,
			self.cigar]

	def tostring(self):
		return '\t'.join(str(x) for x in [
			self.prmType,
			self.prmFlag,
			self.readID,
			self.startAln,
			self.endAln,
			self.prmSize,
			self.alnErr,
			self.winInd,
			self.winNum,
			self.cigar])


def groupPrimers(matchList):
	# Split by primer type to ensure we only combine primes of the same sort
	for side in set(x.prmType for x in matchList):
		thisSide = [x for x in matchList if x.prmType == side]
		if len(thisSide) > 1:
			# Merge latter listed reads into earlier reads if overlapping
			for j in xrange(len(thisSide)-1, -1, -1):
				for i in xrange(j-1, -1, -1):
					curRead = thisSide[i]
					nextRead = thisSide[j]

					# Allow a slight offset to consider overlapping to identify
					# win_i and win_i+2 correctly, and check if both are on the
					# same strand
					if curRead.endAln + 10 >= nextRead.startAln \
							and curRead.startAln <= nextRead.endAln + 10 \
							and curRead.prmFlag&(1<<4) == nextRead.prmFlag&(1<<4):
						# Merge information
						curRead.startAln = min(curRead.startAln,nextRead.startAln)
						curRead.endAln = max(curRead.endAln,nextRead.endAln)
						curRead.winInd.extend(nextRead.winInd)
						curRead.alnErr.extend(nextRead.alnErr)
						curRead.cigar.extend(nextRead.cigar)
						# Remove last item in list of merged information parts
						matchList.remove(nextRead)
						# Merge further in subsequent loops, not in this one
						break

	return matchList


def findCuts(matchList):
	# Ensure the list provided is sorted by start positions
	matchList.sort(key=lambda x: x.startAln)
	cutList = []

	if matchList == []:
		return cutList

	# If the first primer points to the start, accept it
	if matchList[0].prmFlag&(1<<4)==16:
		cutList.append([None,matchList[0].startAln,0,matchList[0].prmType])

	# Any two subsequent primers pointing toward eachother are accepted,
	# but ensure they are not the same primer
	for i in xrange(0,len(matchList)-1):
		if matchList[i].prmFlag&(1<<4)==0 and \
				matchList[i+1].prmFlag&(1<<4)==16 and \
				matchList[i].prmType != matchList[i+1].prmType:
			cutList.append([matchList[i].endAln,matchList[i+1].startAln,matchList[i].prmType,matchList[i+1].prmType])
		# TODO: Check if we need to add +1 to the end position above
		# If we want to remove reads where 2 of the same primer type are
		# pointing toward eachother we could return an empty list here

	# If the last primer points to the end, accept it
	if matchList[-1].prmFlag&(1<<4)==0:
		cutList.append([matchList[-1].endAln,None,matchList[-1].prmType,0])

	return cutList


def combinePrimers(insam,prmLen,qualThreshold=.20):
	samfile = pysam.AlignmentFile(insam, "rb")

	# Create a dict with lists of SimpleRead using referenceName as key
	myDict = collections.defaultdict(list)
	for read in samfile:
		myDict[int(dict(item.split(":") for item in read.reference_name.split(";"))['Rd.Id'])].append(SimpleRead(read,prmLen))

	# Put data in referenceName sorted order
	sortedKeys = myDict.keys()
	sortedKeys.sort(key=int)
	cutAllList = []

	for key in sortedKeys:
		# Remove low quality matches
		matchedRead = [x for x in myDict[key] if (x.alnErr[0] / float(x.endAln-x.startAln) < qualThreshold)]

		# Group overlapping primers on a read
		grouped = groupPrimers(matchedRead)

		# Determine where cuts should be based on mapped primers
		cutAllList.append([key,findCuts(grouped)])

	return cutAllList


def applyCuts(inFile,outFile,cutList,primerSeqs,cutDesc='Cr'):
	readId=-1
	readName=''
	readSeq=''
	cutId = 1
	with open(inFile,'r') as fqFile, open(outFile,'w') as dumpFile:

		# Indexing is lead by cutlist, contains less than or equal to inFile
		for cut in cutList:

			# Assuming both lists are sorted by read id (int),
		 	# play catchup between the two lists
			while cut[0] > readId:
				readName=fqFile.next().rstrip()
				readSeq=fqFile.next().rstrip()
				readPlus=fqFile.next().rstrip()
				readPhred=fqFile.next().rstrip()
				readId=int(dict(item.split(":") for item in readName[1:].split(";"))['Rd.Id'])

			# Both lists are now either aligned or inFile is ahead
			if readId == cut[0]:

				# No cuts can be made, take whole sequence instead
				if cut[1] == []:
					cut[1] = [[0,len(readSeq),0,0]]

				# Split sequence, dump information
				for i,x in enumerate(cut[1]):
					if x[0] == None:
						x[0] = 0
					if x[1] == None:
						x[1] = len(readSeq)

					# Extend the identifier
					dumpFile.write(readName + ';' +
						cutDesc + '.Id:' + str(cutId) + ';' +
						cutDesc + '.SBp:' + str(x[0]) + ';' +
						cutDesc + '.EBp:' + str(x[1]) + ';' +
						cutDesc + '.SPr:' + str(x[2]) + ';' +
						cutDesc + '.EPr:' + str(x[3]) +
						'\n')#+':'+str(x[0])+'-'+str(x[1])

					# Dump the actual sub sequence with primers
					dumpFile.write(str(Seq(primerSeqs[x[2]])) +
						readSeq[x[0]:x[1]] +
						str(Seq(primerSeqs[x[3]]).reverse_complement()) +
						'\n')
					dumpFile.write(readPlus+'\n')

					# Add perfect phred scores for forced primers
					dumpFile.write('~'*len(primerSeqs[x[2]]) +
						readPhred[x[0]:x[1]] +
						'~'*len(primerSeqs[x[3]]) +
						'\n')

					cutId += 1


### splitreads implementation ###

def findRestrictionSeqs(inFile,outFile,restSeqs,cutDesc='Fr'):
	# compRestSeqs = [str(Seq(x).reverse_complement()) for x in restSeqs]
	# restSeqs.extend(compRestSeqs)
	#restSeqs.sort(key=lambda item: (-len(item), item))
	reSeqs='|'.join(restSeqs)
	cutList = []
	cutId = 1

	with open(inFile,'r') as fqFile, open(outFile,'w') as dumpFile:
		for read in fqFile:
			readName=read.rstrip() #faFile.next().rstrip()
			readSeq=fqFile.next().rstrip()
			readPlus=fqFile.next().rstrip()
			readPhred=fqFile.next().rstrip()

			matches = [[x.start(), x.end(), x.group()] for x in (re.finditer(reSeqs, readSeq))]
			thisCut = []
			if matches != []:
				thisCut.append((0,matches[0][1],
					-1,restSeqs.index(matches[0][2])))
				for i in xrange(len(matches)-1):
					thisCut.append((matches[i][0],matches[i+1][1],
						restSeqs.index(matches[i][2]),restSeqs.index(matches[i+1][2])))
				thisCut.append((matches[-1][0],len(readSeq),
					restSeqs.index(matches[-1][2]),-1))
			else:
				thisCut.append((0,len(readSeq),-1,-1))

			# Split sequence, dump information
			for i,x in enumerate(thisCut):

				# Extend the identifier
				#dumpFile.write(readName+';'+cutDesc+':'+str(i)+'\n')#+':'+str(x[0])+'-'+str(x[1])
				dumpFile.write(readName + ';' +
					cutDesc + '.Id:' + str(cutId)   + ';' +
					cutDesc + '.SBp:' + str(x[0])   + ';' +
					cutDesc + '.EBp:' + str(x[1])   + ';' +
					cutDesc + '.SRs:' + str(x[2]+1) + ';' +
					cutDesc + '.ERs:' + str(x[3]+1) +
					'\n')#+':'+str(x[0])+'-'+str

				# Dump the actual sub sequence
				dumpFile.write(readSeq[x[0]:x[1]]+'\n')
				dumpFile.write(readPlus+'\n')
				dumpFile.write(readPhred[x[0]:x[1]]+'\n')

				cutId += 1

			cutList.append([readName,thisCut])

	return cutList


### extending mapped read parts to restriction sites ###

def findReferenceRestSites(refFile,restSeqs,lineLen=50):
	reSeqs='|'.join(restSeqs)
	restSitesDict = dict()

	with open(refFile,'r') as reference:
		curChrom = None
		offset = -lineLen
		matches = []
		readSeq = ''
		for line in reference:
			if line[0] == '>':
				matches.extend([[x.start()+offset, x.end()+offset] for x in (re.finditer(reSeqs, readSeq)) if x.start()])
				readSeq = 'N'*lineLen*2
				offset = -lineLen
				matches = []
				restSitesDict[line[1:].rsplit()[0]] = matches
			else:
				readSeq = readSeq[lineLen:]+line.rsplit()[0].upper()
				matches.extend([[x.start()+offset, x.end()+offset] for x in (re.finditer(reSeqs, readSeq)) if x.start() < lineLen])
				offset += lineLen
		matches.extend([[x.start()+offset, x.end()+offset] for x in (re.finditer(reSeqs, readSeq)) if x.start()])

	return restSitesDict



### combine and export data for plotting tools ###


def mapToRefSite(refSiteList,mappedPos):
	# Use bisect implementation to quickly find a matching position
	pos = bs.bisect_left(refSiteList,mappedPos)
	refLen = len(refSiteList)-1

	# Don't bother beyond the last position in the list
	pos = min(pos, refLen)

	# Move start and end positions to match our heuristic
	left = pos
	right = pos
	while refSiteList[left][0] >= mappedPos[0] + 10 and left > 0:
		left -= 1
	while refSiteList[right][1] <= mappedPos[1] - 10 and right < refLen:
		right += 1

	return [left, right]

def exportToPlot(restrefs,insam,uniqid=['Cr.Id'],minqual=20):
	#insam = sys.argv[1]
	samfile = pysam.AlignmentFile(insam, "rb")

	prevRead = samfile.next()
	prevResult = [-1,-1]
	prevID = ''
	curID = ''
	curStack = []
	curSplit = None
	# byReads = []

	byReads = collections.defaultdict(list)

	headers = []
	readIDs = []
	readInfos = []

	for read in samfile:
		if not read.is_unmapped and read.mapping_quality >= minqual:
			if read.reference_name not in restrefs:
				continue
			result = mapToRefSite(restrefs[read.reference_name],[read.reference_start, read.reference_start + read.infer_query_length(always=True)])

			# Treat main read + primer cleave information together as unique read id
			curSplit = [item.split(":") for item in read.query_name.split(";")]
			curDict = dict(curSplit)
			curID = ';'.join([curDict[x] for x in uniqid])

			# Determine soft clipped basepairs at start and end of mapping
			leftSkip = 0
			rightSkip = 0
			if read.cigartuples[0][0] == 4:
				leftSkip = read.cigartuples[0][1]
			if read.cigartuples[-1][0] == 4:
				rightSkip = read.cigartuples[-1][1]

			curInfo = [
				read.reference_name,
				read.reference_start,
				read.reference_start + read.infer_query_length(always=True),
				read.is_reverse,
				leftSkip,
				rightSkip,
				result[0],
				result[1],
				True
				]

			# TODO are we missing out on a sanity check here? Say, sites 1,2,1 for example?
			# If two subsequent reads:
				# were mapped to the same chromosome,
				# and to the same strand,
				# and with at least one overlapping restriction site
				# and have the same mother read...
			if prevRead.reference_id == read.reference_id \
				and prevRead.is_reverse == read.is_reverse \
				and result[0] <= prevResult[1] and result[1] >= prevResult[0] \
				and curID == prevID:
					 curStack.append((read,result))
					 curInfo[-1] = False
			else:
				if curStack != []:
					for i in range(min([x[1][0] for x in curStack]), max([x[1][1] for x in curStack])+1):
						restrefs[prevRead.reference_name][i].append((prevID,prevRead.is_reverse))
				curStack = [(read,result)]

			readIDs.append(curID)
			readInfos.append([int(x[1]) for x in curSplit] + curInfo)

			prevRead = read
			prevResult = result
			prevID = curID

	# Once more to empty the stack
	for i in range(min([x[1][0] for x in curStack]), max([x[1][1] for x in curStack])+1):
		restrefs[prevRead.reference_name][i].append((prevID,prevRead.is_reverse))

	headers = [x[0] for x in curSplit]
	headerConvert = {
		'Fl.Id'  : 'FileID',
		'Rd.Id'  : 'ReadId',
		'Cr.Id'  : 'CircleId',
		'Cr.SBp' : 'CircleStartBp',
		'Cr.EBp' : 'CircleEndBp',
		'Cr.SPr' : 'CircleStartPr',
		'Cr.EPr' : 'CircleEndPr',
		'Fr.Id'  : 'FragmentId',
		'Fr.SBp' : 'FragmentStartBp',
		'Fr.EBp' : 'FragmentEndBp',
		'Fr.SRs' : 'FragmentStartRes',
		'Fr.ERs' : 'FragmentEndRes',
	}

	for i,val in enumerate(headers):
		headers[i] = headerConvert[val]
	headers.extend(['AlnChr','AlnStart','AlnEnd','AlnStrand','AlnSkipLeft','AlnSkipRight','ExtStart','ExtEnd','ExtLig'])

	pdFrame = pd.DataFrame(readInfos, index=readIDs, columns=headers)

	for key in restrefs:
		curList = restrefs[key]
		keyLen = len(curList)-1

		# Turn restriction site locations into meaningful regions
		for i in xrange(0,keyLen):
			curList[i][0] = sum(curList[i][:2])/2
			curList[i][1] = sum(curList[i+1][:2])/2

		# Remove empty values from the dataset
		for x in xrange(keyLen,-1,-1):
			if len(curList[x]) <= 2:
				curList.pop(x)
			else:
				curList[x] = ((curList[x][0],curList[x][1]),curList[x][2:])

		# Make links from read indexes to regions
		for i,val in enumerate(curList):
			for x in val[1]:
				byReads[x[0]].append((key,i))

	return restrefs,byReads,pdFrame
	#np.savez_compressed(sys.argv[3],byregion=restrefs,byread=dict(byReads))
