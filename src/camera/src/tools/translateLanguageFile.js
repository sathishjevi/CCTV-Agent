console.log('This translation tool uses Yandex.')
if(!process.argv[2]||!process.argv[3]||!process.argv[4]){
    console.log('You must input arguments.')
    console.log('# node translateLanguageFile.js <SOURCE> <FROM_LANGUAGE> <TO_LANGUAGE>')
    console.log('Example:')
    console.log('# node translateLanguageFile.js en_US en ar')
    return
}
var langDir='../languages/'
var fs=require('fs');
var https = require('https');
var jsonfile=require('jsonfile');
var source=require(langDir+process.argv[2]+'.json')
var list = Object.keys(source)
console.log(list.length)
var extra = ''
var current = 1
var currentItem = list[0]
var chosenFile = langDir+process.argv[4]+'.json'
try{
    newList=require(chosenFile)
}catch(err){
    console.log(chosenFile)
    var newList={}
}
var newListAlphabetical={}
var goNext=function(){
    ++current
    currentItem = list[current]
    if(list.length===current){
        console.log('complete checking.. please wait')
            Object.keys(newList).sort().forEach(function(y,t){
                newListAlphabetical[y]=newList[y]
            })
            jsonfile.writeFile(chosenFile,newListAlphabetical,{spaces: 2},function(){
                console.log('complete writing')
            })
    }else{
        next(currentItem)
    }
}
var next=function(v){
    if(v===undefined){return false}
    if(newList[v]&&newList[v]!==source[v]){
        goNext()
        return
    }
    if(/<[a-z][\s\S]*>/i.test(source[v])===true){
        extra+='&format=html'
    }
    var url = 'https://translate.yandex.net/api/v1.5/tr.json/translate?key=trnsl.1.1.20160311T042953x.3xxxxx38abcd6.c7xxxxxccc7f57160141021caxxxxxxd379'+extra+'&lang='+process.argv[3]+'-'+process.argv[4]+'&text='+source[v]
    https.request(url, function(data) {
        data.setEncoding('utf8');
        var chunks='';
        data.on('data', (chunk) => {
            chunks+=chunk;
        });
        data.on('end', () => {
            try{
                chunks=JSON.parse(chunks)
                if(chunks.html){
                    if(chunks.html[0]){
                        var translation=chunks.html[0]
                    }else{
                        var translation=chunks.html
                    }
                    
                }else{
                    var translation=chunks.text[0]
                }
            }catch(err){
                var translation=source[v]
            }
            newList[v]=translation;
            console.log(current+'/'+list.length+','+v+' ---> '+translation)
            goNext()
        });
    }).on('error', function(e) {
        console.log('ERROR : 500 '+v)
        res.sendStatus(500);
    }).end();
}
next(currentItem)